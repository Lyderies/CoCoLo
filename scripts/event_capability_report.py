#!/usr/bin/env python3
"""Summarise the recorded frame shapes of real child runs into an event capability report.

The shapes are recorded only while the owner's flag file exists:

    <state root>/diagnostics/capture-event-shapes

and are kept, one file per child run, in `<state root>/diagnostics/event-shapes/`.
They hold names, counts, rounded sizes and time offsets; no content. This script
reads them and prints one section per provider, client version, model and effort:
startup, reasoning, tool use, quiet periods, the final result and the frame shapes
the liveness rules rely on. It starts no client and changes nothing.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import statistics
import sys

# The liveness thresholds a gap is compared with: quiet, stale and the silence stop.
THRESHOLDS = (("over 30 s", 30_000), ("over 2 min (quiet)", 120_000), ("over 5 min (stale)", 300_000),
              ("over 10 min (stopped for silence)", 600_000))


def default_state_root():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "codex-agents"


def read_run(path):
    """One run's lines; a file that is not a shape record is skipped."""
    lines = []
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                value = json.loads(raw)
                if type(value) is not dict or type(value.get("ms")) is not int:
                    return None
                lines.append(value)
    except (OSError, ValueError):
        return None
    if not lines or lines[0].get("kind") != "start" or lines[0].get("schema") != 1:
        return None
    return lines


def label(frame):
    name = str(frame.get("type"))
    if "subtype" in frame:
        name += "/" + str(frame["subtype"])
    event, item = frame.get("event"), frame.get("item")
    if type(event) is dict:
        name += ":" + str(event.get("type"))
        if event.get("delta") is not None:
            name += "/" + str(event["delta"])
        elif type(event.get("block")) is dict:
            name += "/" + str(event["block"].get("type"))
    if type(item) is dict:
        name += ":" + str(item.get("type"))
    return name


def blocks(frame):
    message = frame.get("message")
    return message.get("blocks", []) if type(message) is dict else []


def is_work(provider, frame):
    """A frame the liveness rules could count as work (the rules themselves also check identity and order)."""
    if provider == "codex":
        return type(frame.get("item")) is dict
    kind = frame.get("type")
    if kind == "assistant":
        return any(block.get("type") in {"text", "thinking", "tool_use"} for block in blocks(frame))
    if kind == "user":
        return any(block.get("type") == "tool_result" for block in blocks(frame))
    event = frame.get("event")
    return (kind == "stream_event" and type(event) is dict and event.get("type") == "content_block_delta"
            and event.get("delta") in {"text_delta", "thinking_delta", "input_json_delta"} and event.get("blank") is False)


def spread(values):
    if not values:
        return "not seen"
    return f"min {min(values)} ms, median {int(statistics.median(values))} ms, max {max(values)} ms"


def longest_gaps(times):
    return [later - earlier for earlier, later in zip(times, times[1:])]


class Group:
    def __init__(self):
        self.runs = 0
        self.truncated = 0
        self.frames = Counter()
        self.keys = defaultdict(set)
        self.other_keys = Counter()
        self.first_frame, self.first_work = [], []
        self.frame_gaps, self.work_gaps = [], []
        self.tools, self.outcomes, self.results = Counter(), Counter(), Counter()
        self.partial = Counter()
        self.facts = Counter()
        self.durations = []

    def add(self, provider, lines):
        self.runs += 1
        frames = [line for line in lines if line.get("kind") == "frame"]
        self.truncated += any(line.get("kind") == "truncated" for line in lines)
        end = next((line for line in lines if line.get("kind") == "end"), None)
        self.outcomes[str(end.get("outcome")) if end else "not recorded"] += 1
        if end:
            self.durations.append(end["ms"])
        if frames:
            self.first_frame.append(frames[0]["ms"])
        work = [frame["ms"] for frame in frames if is_work(provider, frame)]
        if work:
            self.first_work.append(work[0])
        closing = [end["ms"]] if end else []
        self.frame_gaps += longest_gaps([frame["ms"] for frame in frames] + closing)
        self.work_gaps += longest_gaps(work + closing)
        for frame in frames:
            name = label(frame)
            self.frames[name] += 1
            self.keys[name] |= set(frame.get("keys", []))
            self.other_keys[name] = max(self.other_keys[name], frame.get("other_keys", 0))
            if "other" in name:
                self.facts["frames with a name outside the known lists"] += 1
            for block in blocks(frame):
                if block.get("type") == "tool_use":
                    self.tools[str(block.get("tool"))] += 1
                elif block.get("type") == "tool_result":
                    self.facts["Tool results"] += 1
                elif block.get("type") in {"thinking", "redacted_thinking"}:
                    self.facts["Whole thinking blocks"] += 1
            event = frame.get("event")
            if frame.get("type") == "stream_event":
                self.partial["Partial frames"] += 1
                self.partial["With a UUID of their own" if frame.get("uuid") == "uuid" else
                             "With another string ID" if frame.get("uuid") == "string" else "Without a usable ID"] += 1
                self.partial["Repeating an earlier frame's ID"] += frame.get("uuid_repeated") is True
                self.partial["Naming their API message"] += frame.get("has_api_message_id") is True
                if type(event) is dict and event.get("delta") == "thinking_delta":
                    self.facts["Thinking fragments"] += 1
            item = frame.get("item")
            if type(item) is dict:
                if item.get("type") == "reasoning":
                    self.facts["Reasoning items"] += 1
                if item.get("type") == "command_execution":
                    self.tools["command_execution"] += 1
            if frame.get("type") == "result":
                self.results[f"{frame.get('subtype')}, structured output "
                             f"{'present' if frame.get('structured_output') else 'absent'}"] += 1
            if frame.get("type") in {"turn.completed", "turn.failed"}:
                self.results[str(frame.get("type"))] += 1

    def render(self, title):
        out = [f"## {title}", "", f"Runs recorded: {self.runs}" + (f" ({self.truncated} cut at the line limit)"
                                                                   if self.truncated else ""), ""]
        out += ["### Startup", "", f"- First frame after launch: {spread(self.first_frame)}",
                f"- First work frame after launch: {spread(self.first_work)}", ""]
        out += ["### Reasoning and tool use", ""]
        for name in ("Thinking fragments", "Whole thinking blocks", "Reasoning items", "Tool results"):
            out.append(f"- {name}: {self.facts.get(name, 0)}")
        out.append("- Tool calls: " + (", ".join(f"{name} x{count}" for name, count in sorted(self.tools.items()))
                                       or "none"))
        out += ["", "### Quiet periods", "",
                f"- Longest gap between any two frames: {max(self.frame_gaps, default=0)} ms",
                f"- Longest gap between two work frames (or the last one and the end): "
                f"{max(self.work_gaps, default=0)} ms"]
        for name, limit in THRESHOLDS:
            out.append(f"- Work gaps {name}: {sum(gap > limit for gap in self.work_gaps)}")
        out += ["", "### Final result", "", f"- Run length: {spread(self.durations)}"]
        out += [f"- {name}: {count}" for name, count in sorted(self.results.items())] or ["- No result frame seen"]
        out += [f"- Ended as {name}: {count}" for name, count in sorted(self.outcomes.items())]
        if self.partial:
            out += ["", "### Partial frames", ""]
            out += [f"- {name}: {count}" for name, count in self.partial.items()]
        out += ["", "### Frame kinds", "", "| Frame | Count | Fields | Fields outside the known list |", "|---|---|---|---|"]
        for name, count in sorted(self.frames.items()):
            out.append(f"| `{name}` | {count} | {', '.join(sorted(self.keys[name]))} | {self.other_keys[name]} |")
        unknown = self.facts.get("frames with a name outside the known lists", 0)
        out += ["", f"Frames with a name outside the known lists: {unknown}", ""]
        return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--codex-version", default="not stated",
                        help="the Codex client's version, which its frames do not carry (codex --version)")
    args = parser.parse_args(argv)
    folder = (args.state_root or default_state_root()) / "diagnostics" / "event-shapes"
    groups, skipped = defaultdict(Group), 0
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        lines = read_run(path)
        if lines is None:
            skipped += 1
            continue
        head = lines[0]
        provider = str(head.get("provider"))
        version = next((line["client_version"] for line in lines if "client_version" in line),
                       args.codex_version if provider == "codex" else "not reported")
        groups[(provider, str(version), str(head.get("model")), str(head.get("effort")))].add(provider, lines)
    out = ["# Event capability report", "",
           "Built from recorded frame shapes of real child runs: names, counts, rounded sizes and time",
           "offsets only. Times are milliseconds since the child was launched.", ""]
    if not groups:
        out += [f"No recorded runs were found in {folder}.", ""]
    for key in sorted(groups):
        provider, version, model, effort = key
        out += groups[key].render(f"{provider} client {version}, model {model}, effort {effort}")
    if skipped:
        out += [f"Files skipped because they are not shape records: {skipped}", ""]
    sys.stdout.write("\n".join(out))
    return 0 if groups else 1


if __name__ == "__main__":
    raise SystemExit(main())
