from __future__ import annotations
from tests.support import SHORT_TMP

import io
import json
import os
from pathlib import Path
import runpy
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import tests.support  # noqa: F401
from tests.support import REPOSITORY_ROOT
import tests.test_bridge as bridge_fixtures

from codex_agents import bridge, event_shapes

MODEL = "claude-opus-5-5"
CANARY = "PRIVATE-CANARY"
REPORT = REPOSITORY_ROOT.parent.parent / "scripts" / "event_capability_report.py"


def stream(kind, uuid, **event):
    return {"type": "stream_event", "event": {"type": kind, **event}, "parent_tool_use_id": None,
            "session_id": CANARY, "uuid": uuid}


def identity(number):
    return f"00000000-0000-4000-8000-{number:012d}"


CLAUDE_FRAMES = [
    bridge_fixtures.init_frame(claude_code_version="2.1.311", cwd="/" + CANARY),
    stream("message_start", identity(1), message={"id": CANARY, "model": MODEL, "content": []}),
    stream("content_block_start", identity(2), index=0, content_block={"type": "thinking", "thinking": ""}),
    stream("content_block_delta", identity(3), index=0, delta={"type": "thinking_delta", "thinking": CANARY}),
    stream("content_block_delta", identity(3), index=0, delta={"type": "thinking_delta", "thinking": CANARY}),
    stream("content_block_start", identity(4), index=1, content_block={
        "type": "tool_use", "id": CANARY, "name": "Read", "input": {}}),
    stream("content_block_delta", identity(5), index=1, delta={"type": "input_json_delta", "partial_json": CANARY}),
    {"type": "assistant", "uuid": identity(6), "session_id": CANARY, "parent_tool_use_id": None, "message": {
        "id": CANARY, "model": MODEL, "role": "assistant", "stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": CANARY, "signature": CANARY},
            {"type": "tool_use", "id": CANARY, "name": "Read", "input": {"file_path": CANARY}},
            {"type": "tool_use", "id": CANARY, "name": CANARY, "input": {}}]}},
    {"type": "user", "uuid": identity(7), "session_id": CANARY, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": CANARY, "content": CANARY}]}, CANARY: CANARY},
    {"type": CANARY, "subtype": CANARY, "uuid": CANARY},
    {**bridge_fixtures.result_frame(CANARY), "uuid": identity(8), "num_turns": 2, "structured_output": {CANARY: 1}},
]
CODEX_FRAMES = [
    {"type": "thread.started", "thread_id": CANARY},
    {"type": "item.completed", "item": {"id": CANARY, "type": "reasoning", "text": CANARY}},
    {"type": "item.completed", "item": {"id": CANARY, "type": "command_execution", "command": CANARY,
                                        "aggregated_output": CANARY, "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": CANARY, "type": CANARY, "text": CANARY}},
    {"type": "turn.completed", "usage": {"input_tokens": 1}},
]


class EventShapeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.diagnostics = self.state / "diagnostics"
        self.diagnostics.mkdir(mode=0o700)
        event_shapes.configure(self.diagnostics)
        self.addCleanup(event_shapes.configure, None)

    def ask(self):
        (self.diagnostics / event_shapes.FLAG).touch(mode=0o600)

    def record(self, provider, frames, model=MODEL):
        made = event_shapes.recorder(provider, requested=model, effort="high", tools=bridge.CLAUDE_TOOLS)
        seen = []
        on_frame = event_shapes.recorded(made, seen.append)
        for frame in frames:
            on_frame(frame)
        if made is not None:
            made.finish("exit")
        self.assertEqual(frames, seen)  # Every frame still reaches the run's own handling, unchanged.
        return made

    def runs(self):
        folder = self.diagnostics / event_shapes.FOLDER
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_nothing_is_recorded_unless_the_owner_asks(self):
        on_frame = object()
        self.assertIsNone(self.record("claude", CLAUDE_FRAMES))
        self.assertIs(on_frame, event_shapes.recorded(None, on_frame))
        self.assertEqual([], self.runs())
        # A flag that is not the owner's plain file, or a folder others can reach, does not count.
        (self.diagnostics / "elsewhere").touch()
        os.symlink(self.diagnostics / "elsewhere", self.diagnostics / event_shapes.FLAG)
        self.assertIsNone(self.record("claude", CLAUDE_FRAMES))
        os.unlink(self.diagnostics / event_shapes.FLAG)
        self.ask()
        os.chmod(self.diagnostics, 0o755)
        self.assertIsNone(self.record("claude", CLAUDE_FRAMES))
        os.chmod(self.diagnostics, 0o700)
        event_shapes.configure(None)
        self.assertIsNone(self.record("claude", CLAUDE_FRAMES))
        self.assertEqual([], self.runs())

    def test_shapes_hold_names_counts_and_sizes_but_no_content(self):
        self.ask()
        self.record("claude", CLAUDE_FRAMES)
        self.record("codex", CODEX_FRAMES, model="gpt-6-astra")
        claude, codex = self.runs()
        for path in (claude, codex):
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertNotIn(CANARY, path.read_text())
        self.assertEqual(0o700, stat.S_IMODE(claude.parent.stat().st_mode))
        lines = [json.loads(line) for line in claude.read_text().splitlines()]
        self.assertEqual({"kind": "start", "schema": 1, "provider": "claude", "model": MODEL, "effort": "high"},
                         {key: value for key, value in lines[0].items() if key != "ms"})
        self.assertEqual(["start"] + ["frame"] * len(CLAUDE_FRAMES) + ["end"], [line["kind"] for line in lines])
        frames = lines[1:-1]
        self.assertEqual(("system", "init", "2.1.311", 3), (
            frames[0]["type"], frames[0]["subtype"], frames[0]["client_version"], frames[0]["tools_count"]))
        thinking = frames[3]
        self.assertEqual(("uuid", False, "thinking_delta", len(CANARY).bit_length(), False), (
            thinking["uuid"], thinking["uuid_repeated"], thinking["event"]["delta"], thinking["event"]["size"],
            thinking["event"]["blank"]))
        self.assertTrue(frames[4]["uuid_repeated"])  # The same frame ID a second time is visible as such.
        self.assertEqual({"type": "tool_use", "tool": "Read", "has_id": True}, frames[5]["event"]["block"])
        self.assertEqual(["thinking", "tool_use", "tool_use"], [b["type"] for b in frames[7]["message"]["blocks"]])
        self.assertEqual(["Read", "other"], [b["tool"] for b in frames[7]["message"]["blocks"][1:]])
        self.assertEqual("requested", frames[7]["message"]["model"])
        self.assertEqual(1, frames[8]["other_keys"])  # A field outside the known list is counted, not named.
        self.assertEqual(("other", "other", "string"), (frames[9]["type"], frames[9]["subtype"], frames[9]["uuid"]))
        self.assertEqual(("result", "success", True, 2), (
            frames[10]["type"], frames[10]["subtype"], frames[10]["structured_output"], frames[10]["num_turns"]))
        self.assertEqual("exit", lines[-1]["outcome"])
        items = [json.loads(line).get("item") for line in codex.read_text().splitlines()]
        self.assertEqual(["reasoning", "command_execution", "other"], [item["type"] for item in items if item])
        self.assertTrue(items[3]["exit_zero"])

    def test_recording_failures_and_limits_never_reach_the_run(self):
        self.ask()
        with mock.patch.object(event_shapes, "MAX_LINES", 2):
            self.record("claude", CLAUDE_FRAMES)
        lines = [json.loads(line) for line in self.runs()[0].read_text().splitlines()]
        self.assertEqual(["start", "frame", "frame", "truncated", "end"], [line["kind"] for line in lines])
        with mock.patch.object(event_shapes, "MAX_RUN_BYTES", 1):
            self.record("claude", CLAUDE_FRAMES)
        self.assertEqual(2, len(self.runs()))
        with mock.patch.object(event_shapes, "frame_shape", side_effect=RuntimeError(CANARY)):
            self.record("claude", CLAUDE_FRAMES)  # Off for that run: nothing is written for it.
        with mock.patch.object(event_shapes.os, "open", side_effect=OSError(CANARY)):
            self.record("claude", CLAUDE_FRAMES)
        self.assertEqual(2, len(self.runs()))
        for unusual in ([], None, {"type": []}, {"type": "assistant", "message": {"content": [CANARY, 3]}},
                        {"type": "stream_event", "event": {"type": [], "delta": {"type": {}}, "index": -1}}):
            self.record("claude", [unusual])
        for path in self.runs():
            self.assertNotIn(CANARY, path.read_text())

    def test_no_file_is_touched_while_the_child_runs(self):
        self.ask()
        made = event_shapes.recorder("claude", requested=MODEL, effort="high", tools=bridge.CLAUDE_TOOLS)
        on_frame = event_shapes.recorded(made, lambda frame: None)
        # From here to the child's end the reader's path does no file work at all.
        with mock.patch.object(event_shapes.os, "open", side_effect=AssertionError("opened")), \
                mock.patch.object(event_shapes.os, "write", side_effect=AssertionError("written")), \
                mock.patch.object(event_shapes.os, "lstat", side_effect=AssertionError("looked")), \
                mock.patch.object(event_shapes.os, "scandir", side_effect=AssertionError("listed")):
            for frame in CLAUDE_FRAMES * 50:
                on_frame(frame)
        self.assertEqual([], self.runs())
        event_shapes.finished(made, {"abort": "work_silence"})
        lines = [json.loads(line) for line in self.runs()[0].read_text().splitlines()]
        self.assertEqual((len(CLAUDE_FRAMES) * 50 + 2, "stopped"), (len(lines), lines[-1]["outcome"]))
        event_shapes.finished(made, {"abort": None})  # Written once.
        self.assertEqual(1, len(self.runs()))

    def test_recording_stops_when_the_folder_is_full(self):
        self.ask()
        self.record("claude", CLAUDE_FRAMES)
        with mock.patch.object(event_shapes, "MAX_FILES", 1):
            self.assertIsNone(self.record("claude", CLAUDE_FRAMES))
        with mock.patch.object(event_shapes, "MAX_FOLDER_BYTES", 10):
            self.assertIsNone(self.record("codex", CODEX_FRAMES, model="gpt-6-astra"))
        self.assertEqual(1, len(self.runs()))
        self.assertIsNotNone(self.record("codex", CODEX_FRAMES, model="gpt-6-astra"))

    def test_the_bridge_records_every_frame_before_its_fences(self):
        self.ask()
        seen = []
        with mock.patch.object(bridge, "run_child", side_effect=lambda arguments, *, on_frame, **_: [
                on_frame(frame) for frame in CODEX_FRAMES[:2]] and {"kind": "exit", "returncode": 0}):
            try:
                bridge.run_codex(
                    ledger=object(), project_id="p" * 64, route={"model": "gpt-6-astra", "effort": "high"},
                    assignment=bridge_fixtures.assignment(), role_prompt="Read only.", schema=b"{}",
                    export=self.state, control=self.state, environ={"PATH": os.defpath},
                    deadline_seconds=5, executable="/usr/bin/false")
            except Exception as error:  # The stubbed child returns no result; only the record matters here.
                seen.append(error)
        lines = [json.loads(line) for line in self.runs()[0].read_text().splitlines()]
        self.assertEqual(["start", "frame", "frame", "end"], [line["kind"] for line in lines])
        self.assertEqual(("codex", "exit"), (lines[0]["provider"], lines[-1]["outcome"]))

    def test_the_report_summarises_what_was_recorded(self):
        self.ask()
        self.record("claude", CLAUDE_FRAMES)
        self.record("codex", CODEX_FRAMES, model="gpt-6-astra")
        folder = self.diagnostics / event_shapes.FOLDER
        (folder / "note.jsonl").write_text("not a record\n")
        # Files put there by hand are read as untrusted: nothing in them reaches the report.
        genuine = sorted(folder.glob("claude-*.jsonl"))[0].read_text().splitlines()
        crafted = json.loads(genuine[0])
        crafted.update(provider="claude", model="\x1b[2J" + CANARY, effort=["x"])
        frame = {"kind": "frame", "ms": 1, "type": CANARY + "\x1b]0;", "keys": [CANARY, 7], "other_keys": "many",
                 "subtype": CANARY, "client_version": CANARY,
                 "message": {"blocks": [CANARY, {"type": "tool_use", "tool": CANARY}]}}
        (folder / "crafted.jsonl").write_text("\n".join([json.dumps(crafted), json.dumps(frame)]) + "\n")
        (folder / "unhashable.jsonl").write_text("\n".join([genuine[0], json.dumps(
            {"kind": "frame", "ms": 1, "type": "stream_event", "event": {"type": "content_block_delta", "delta": []}})]))
        (folder / "provider.jsonl").write_text(genuine[0].replace('"claude"', '"' + CANARY + '"') + "\n")
        os.symlink(sorted(folder.glob("codex-*.jsonl"))[0], folder / "linked.jsonl")
        (folder / "nested.jsonl").write_text(genuine[0] + "\n" + "[" * 100_000 + "]" * 100_000 + "\n")
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as done, \
                mock.patch("sys.argv", [str(REPORT), "--state-root", str(self.state), "--codex-version", "0.164.0"]):
            runpy.run_path(str(REPORT), run_name="__main__")
        self.assertEqual(0, done.exception.code)
        report = output.getvalue()
        self.assertIn("## claude client 2.1.311, model claude-opus-5-5, effort high", report)
        self.assertIn("## codex client 0.164.0, model gpt-6-astra, effort high", report)
        self.assertIn("- With a UUID of their own: 6", report)
        self.assertIn("- Repeating an earlier frame's ID: 1", report)
        self.assertIn("- Tool calls: Read x1, other x1", report)
        self.assertIn("- success, structured output present: 1", report)
        self.assertIn("| `stream_event:content_block_delta/thinking_delta` | 2 |", report)
        self.assertIn("Files skipped because they are not shape records: 5", report)
        self.assertIn("## claude client not reported, model other, effort other", report)  # The crafted one.
        self.assertIn("## codex client 0.164.0, model gpt-6-astra, effort high\n\nRuns recorded: 1\n", report)
        self.assertNotIn(CANARY, report)
        self.assertNotIn("\x1b", report)
        empty = io.StringIO()
        with redirect_stdout(empty), self.assertRaises(SystemExit) as nothing, \
                mock.patch("sys.argv", [str(REPORT), "--state-root", str(self.state / "missing")]):
            runpy.run_path(str(REPORT), run_name="__main__")
        self.assertEqual(1, nothing.exception.code)
        self.assertIn("No recorded runs were found", empty.getvalue())


if __name__ == "__main__":
    unittest.main()
