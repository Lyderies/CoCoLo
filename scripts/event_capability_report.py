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
import re
import stat
import statistics
import sys

# The liveness thresholds a gap is compared with: quiet, stale and the silence stop.
THRESHOLDS = (("over 30 s", 30_000), ("over 2 min (quiet)", 120_000), ("over 5 min (stale)", 300_000),
              ("over 10 min (stopped for silence)", 600_000))


def printable(text):
    """Text given on the command line, without anything that could drive a terminal."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:64]


def default_state_root():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "codex-agents"


MAX_RECORD_BYTES = 32 * 1024 * 1024
# The forms of the names the recorder writes. A file put in the folder by hand can at
# most add a short plain word of the same form; anything else is printed as "other".
NAME = re.compile(r"[a-z][a-z0-9_./]{0,47}")
FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
MODEL = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
TOOLS = frozenset({"Read", "Glob", "Grep", "other"})
EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


def name(value, form=NAME):
    return value if type(value) is str and form.fullmatch(value) else "other"


def one_of(value, allowed):
    return value if type(value) is str and value in allowed else "other"


def read_run(path):
    """One run's lines; a file that is not a plain, bounded shape record is skipped.

    The records are the recorder's own, but a file put there by hand is read as
    untrusted: no link is followed, nothing over the size limit is read, and only
    lines of the recorder's form are kept.
    """
    lines = []
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_RECORD_BYTES:
                return None
            for raw in handle.read(MAX_RECORD_BYTES + 1).decode("utf-8").splitlines():
                value = json.loads(raw)
                if (type(value) is not dict or type(value.get("ms")) is not int or not 0 <= value["ms"] < 10 ** 10
                        or value.get("kind") not in {"start", "frame", "end", "truncated"}):
                    return None
                lines.append(value)
    except (OSError, ValueError, RecursionError):
        return None
    if not lines or lines[0].get("kind") != "start" or lines[0].get("schema") != 1:
        return None
    return lines


def label(frame):
    text = name(frame.get("type"))
    if "subtype" in frame:
        text += "/" + name(frame["subtype"])
    event, item = frame.get("event"), frame.get("item")
    if type(event) is dict:
        text += ":" + name(event.get("type"))
        if event.get("delta") is not None:
            text += "/" + name(event["delta"])
        elif type(event.get("block")) is dict:
            text += "/" + name(event["block"].get("type"))
    if type(item) is dict:
        text += ":" + name(item.get("type"))
    return text


def blocks(frame):
    message = frame.get("message")
    found = message.get("blocks", []) if type(message) is dict else []
    return [block for block in found if type(block) is dict] if type(found) is list else []


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
        self.outcomes[name(end.get("outcome")) if end else "not recorded"] += 1
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
            kind = label(frame)
            self.frames[kind] += 1
            fields, extra = frame.get("keys", []), frame.get("other_keys", 0)
            self.keys[kind] |= {name(field, FIELD) for field in fields} if type(fields) is list else set()
            self.other_keys[kind] = max(self.other_keys[kind], extra if type(extra) is int else 0)
            if "other" in kind:
                self.facts["frames with a name outside the known lists"] += 1
            for block in blocks(frame):
                if block.get("type") == "tool_use":
                    self.tools[one_of(block.get("tool"), TOOLS)] += 1
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
                self.results[f"{name(frame.get('subtype'))}, structured output "
                             f"{'present' if frame.get('structured_output') else 'absent'}"] += 1
            if frame.get("type") in {"turn.completed", "turn.failed"}:
                self.results[name(frame.get("type"))] += 1

    def render(self, title):
        out = [f"## {title}", "", f"Runs recorded: {self.runs}" + (f" ({self.truncated} cut at the line limit)"
                                                                   if self.truncated else ""), ""]
        out += ["### Startup", "", f"- First frame after launch: {spread(self.first_frame)}",
                f"- First work frame after launch: {spread(self.first_work)}", ""]
        out += ["### Reasoning and tool use", ""]
        for fact in ("Thinking fragments", "Whole thinking blocks", "Reasoning items", "Tool results"):
            out.append(f"- {fact}: {self.facts.get(fact, 0)}")
        out.append("- Tool calls: " + (", ".join(f"{tool} x{count}" for tool, count in sorted(self.tools.items()))
                                       or "none"))
        out += ["", "### Quiet periods", "",
                f"- Longest gap between any two frames: {max(self.frame_gaps, default=0)} ms",
                f"- Longest gap between two work frames (or the last one and the end): "
                f"{max(self.work_gaps, default=0)} ms"]
        for title_, limit in THRESHOLDS:
            out.append(f"- Work gaps {title_}: {sum(gap > limit for gap in self.work_gaps)}")
        out += ["", "### Final result", "", f"- Run length: {spread(self.durations)}"]
        out += [f"- {kind}: {count}" for kind, count in sorted(self.results.items())] or ["- No result frame seen"]
        out += [f"- Ended as {kind}: {count}" for kind, count in sorted(self.outcomes.items())]
        if self.partial:
            out += ["", "### Partial frames", ""]
            out += [f"- {fact}: {count}" for fact, count in self.partial.items()]
        out += ["", "### Frame kinds", "", "| Frame | Count | Fields | Fields outside the known list |", "|---|---|---|---|"]
        for kind, count in sorted(self.frames.items()):
            out.append(f"| `{kind}` | {count} | {', '.join(sorted(self.keys[kind]))} | {self.other_keys[kind]} |")
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
        provider = head.get("provider")
        if type(provider) is not str or provider not in {"claude", "codex"}:
            skipped += 1
            continue
        version = next((line["client_version"] for line in lines
                        if type(line.get("client_version")) is str and VERSION.fullmatch(line["client_version"])),
                       printable(args.codex_version) if provider == "codex" else "not reported")
        try:
            Group().add(provider, lines)  # A record of an unexpected form is skipped whole, not half counted.
        except (AttributeError, KeyError, TypeError, ValueError):
            skipped += 1
            continue
        groups[(provider, version, name(head.get("model"), MODEL), one_of(head.get("effort"), EFFORTS))].add(
            provider, lines)
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
