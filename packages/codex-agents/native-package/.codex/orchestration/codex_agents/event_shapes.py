"""Opt-in, content-free record of the frame shapes a real client sends.

For the owner's event capability report: which frames a pinned client sends, in
what order and how far apart, so the liveness rules can be checked against the
real thing. Off unless the owner creates the flag file in the state root's
``diagnostics`` folder; then each child run of a project service appends one
line per frame to its own file there. A line holds only names from the fixed
lists below, counts, sizes rounded to a power of two and a time offset. No text,
path, identifier, tool input, label outside those lists or provider timestamp
is kept: a name this module does not know is written as ``other``. While a child
runs its shapes are only held in memory, within fixed limits; the file is written
once, after the child has ended, so no disk is touched on the path that reads the
child's output. A failure switches recording off for that run and is not reported,
and nothing is recorded once the folder holds its limit of files or bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

FLAG = "capture-event-shapes"
FOLDER = "event-shapes"
SCHEMA = 1
MAX_LINES = 50_000  # Per run; more than the bridge's stream cap lets a client send.
MAX_RUN_BYTES = 16 * 1024 * 1024
MAX_FILES = 512  # In the folder; the oldest records rotate out (O0c P12).
MAX_FOLDER_BYTES = 256 * 1024 * 1024
MAX_BLOCKS = 32
_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_TYPES = frozenset({
    "system", "assistant", "user", "result", "rate_limit_event", "stream_event",
    "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
    "item.started", "item.updated", "item.completed",
    "model/rerouted", "model_rerouted", "model.rerouted",
})
_SUBTYPES = frozenset({
    "init", "status", "compact_boundary", "api_retry", "hook_started", "hook_progress", "hook_response",
    "success", "error_max_turns", "error_during_execution", "error_max_budget_usd",
    "error_max_structured_output_retries",
})
_EVENTS = frozenset({
    "message_start", "message_delta", "message_stop", "content_block_start", "content_block_delta",
    "content_block_stop", "ping", "error",
})
_DELTAS = frozenset({"text_delta", "thinking_delta", "input_json_delta", "signature_delta", "citations_delta"})
_BLOCKS = frozenset({"text", "thinking", "redacted_thinking", "tool_use", "tool_result", "server_tool_use"})
_STOPS = frozenset({"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"})
_ITEMS = frozenset({
    "agent_message", "reasoning", "command_execution", "file_change", "todo_list", "error",
    "mcp_tool_call", "web_search", "collab_tool_call",
})
_STATUSES = frozenset({"in_progress", "completed", "failed", "declined"})
_KEYS = frozenset({
    "type", "subtype", "uuid", "session_id", "parent_tool_use_id", "event", "message", "ttft_ms",
    "thinking_display", "api_message_id", "abandoned_blocks", "text_runs_joined", "rate_limit_info",
    "result", "is_error", "usage", "modelUsage", "total_cost_usd", "permission_denials", "duration_ms",
    "duration_api_ms", "num_turns", "stop_reason", "structured_output", "model", "tools", "mcp_servers",
    "plugins", "apiKeySource", "cwd", "permissionMode", "claude_code_version", "slash_commands", "agents",
    "skills", "output_style", "error", "timestamp", "isReplay", "isSynthetic", "tool_use_result",
    "index", "delta", "content_block", "id", "role", "content", "stop_sequence", "context_management",
    "container", "stop_details", "text", "thinking", "partial_json", "signature", "name", "input",
    "tool_use_id", "thread_id", "item", "to_model", "status", "command", "aggregated_output", "exit_code",
    "changes", "items", "fast_mode_state", "terminal_reason", "caller",
})
_TEXT_FIELDS = ("text", "thinking", "partial_json", "signature", "aggregated_output", "command")

_directory: Path | None = None


def configure(directory):
    """Name the diagnostics folder for this process; None switches recording off."""
    global _directory
    _directory = None if directory is None else Path(directory)


def _known(value, names):
    if value is None:
        return None
    return value if type(value) is str and value in names else "other"


def _keys(value):
    known = sorted(key for key in value if type(key) is str and key in _KEYS)
    return {"keys": known, "other_keys": len(value) - len(known)}


def _size(value):
    """A length as the power of two that bounds it; None when there is nothing to measure."""
    if type(value) is str:
        return len(value).bit_length()
    if type(value) in {list, dict}:
        return len(value).bit_length()
    return None


def _model(value, requested):
    if value is None:
        return None
    return "requested" if value == requested else "other"


def _block(block, tools):
    if type(block) is not dict:
        return {"type": "other"}
    shape = {"type": _known(block.get("type"), _BLOCKS)}
    for field in ("text", "thinking"):
        if type(block.get(field)) is str:
            shape["size"] = _size(block[field])
    if shape["type"] in {"tool_use", "server_tool_use"}:
        name = block.get("name")
        shape["tool"] = name if type(name) is str and name in tools else "other"
        shape["has_id"] = type(block.get("id")) is str
    if shape["type"] == "tool_result":
        shape["has_id"] = type(block.get("tool_use_id")) is str
        shape["is_error"] = block.get("is_error") is True
        shape["size"] = _size(block.get("content"))
    return shape


def _message(message, requested, tools):
    shape = {**_keys(message), "model": _model(message.get("model"), requested),
             "has_id": type(message.get("id")) is str, "stop_reason": _known(message.get("stop_reason"), _STOPS)}
    content = message.get("content")
    if type(content) is list:
        shape["blocks"] = [_block(block, tools) for block in content[:MAX_BLOCKS]]
        shape["more_blocks"] = max(0, len(content) - MAX_BLOCKS)
    elif type(content) is str:
        shape["content_size"] = _size(content)
    return shape


def frame_shape(frame, *, requested, tools):
    """The content-free shape of one frame; names outside the fixed lists become ``other``."""
    if type(frame) is not dict:
        return {"type": "other"}
    shape = {"type": _known(frame.get("type"), _TYPES), **_keys(frame)}
    if "subtype" in frame:
        shape["subtype"] = _known(frame["subtype"], _SUBTYPES)
    if "uuid" in frame:
        identity = frame["uuid"]
        shape["uuid"] = ("uuid" if type(identity) is str and _UUID.fullmatch(identity)
                         else "string" if type(identity) is str and identity else "invalid")
    for name in ("model", "to_model"):
        if name in frame:
            shape[name] = _model(frame[name], requested)
    version = frame.get("claude_code_version")
    if type(version) is str and _VERSION.fullmatch(version):
        shape["client_version"] = version
    for name in ("tools", "mcp_servers", "plugins", "permission_denials"):
        if type(frame.get(name)) is list:
            shape[name + "_count"] = min(len(frame[name]), 1024)
    for name in ("is_error", "text_runs_joined"):
        if type(frame.get(name)) is bool:
            shape[name] = frame[name]
    if type(frame.get("num_turns")) is int and 0 <= frame["num_turns"] <= 100_000:
        shape["num_turns"] = frame["num_turns"]
    if "result" in frame:
        shape["result_size"] = _size(frame["result"])
    if "structured_output" in frame:
        shape["structured_output"] = frame["structured_output"] is not None
    if "stop_reason" in frame:
        shape["stop_reason"] = _known(frame["stop_reason"], _STOPS)
    if "error" in frame:
        shape["has_error"] = frame["error"] is not None
    if "api_message_id" in frame:
        shape["has_api_message_id"] = type(frame["api_message_id"]) is str
    event = frame.get("event")
    if type(event) is dict:
        inner = {"type": _known(event.get("type"), _EVENTS), **_keys(event)}
        if type(event.get("index")) is int and 0 <= event["index"] < 4096:
            inner["index"] = event["index"]
        delta = event.get("delta")
        if type(delta) is dict:
            inner["delta"] = _known(delta.get("type"), _DELTAS)
            inner["delta_stop_reason"] = _known(delta.get("stop_reason"), _STOPS)
            for field in _TEXT_FIELDS:
                if type(delta.get(field)) is str:
                    inner["size"] = _size(delta[field])
                    inner["blank"] = not delta[field].strip()
        if type(event.get("content_block")) is dict:
            inner["block"] = _block(event["content_block"], tools)
        if type(event.get("message")) is dict:
            inner["message"] = _message(event["message"], requested, tools)
        shape["event"] = inner
    message = frame.get("message")
    if type(message) is dict:
        shape["message"] = _message(message, requested, tools)
    elif type(message) is str:
        shape["message_size"] = _size(message)
    item = frame.get("item")
    if type(item) is dict:
        inner = {"type": _known(item.get("type"), _ITEMS), **_keys(item),
                 "status": _known(item.get("status"), _STATUSES), "has_id": type(item.get("id")) is str}
        for field in _TEXT_FIELDS:
            if type(item.get(field)) is str:
                inner["size"] = _size(item[field])
        if type(item.get("exit_code")) is int:
            inner["exit_zero"] = item["exit_code"] == 0
        shape["item"] = inner
    return shape


class _Recorder:
    """One run's shapes, held in memory until the child has ended."""

    def __init__(self, path, header, requested, tools):
        self._path = path
        self._requested = requested
        self._tools = frozenset(tools)
        self._started = time.monotonic_ns()
        self._lines = []
        self._bytes = 0
        self._full = False
        self._seen = set()
        self._add(header)

    def _add(self, line):
        line["ms"] = (time.monotonic_ns() - self._started) // 1_000_000
        encoded = json.dumps(line, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self._lines.append(encoded)
        self._bytes += len(encoded)

    def frame(self, frame):
        if self._lines is None or self._full:
            return
        try:
            if len(self._lines) > MAX_LINES or self._bytes >= MAX_RUN_BYTES:
                self._full = True
                self._add({"kind": "truncated"})
                return
            shape = frame_shape(frame, requested=self._requested, tools=self._tools)
            identity = frame.get("uuid") if type(frame) is dict else None
            if type(identity) is str:
                seen = hashlib.sha256(identity.encode()).digest()[:8]
                shape["uuid_repeated"] = seen in self._seen
                self._seen.add(seen)
            self._add({"kind": "frame", **shape})
        except Exception:
            self._lines = None  # Off for this run.

    def finish(self, outcome):
        """Write the record, once; called only after the child has ended."""
        if self._lines is None:
            return
        descriptor = None
        try:
            self._add({"kind": "end", "outcome": _known(outcome, _OUTCOMES)})
            lines, self._lines = self._lines, None
            descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(b"".join(lines))
        except Exception:
            self._lines = None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


_OUTCOMES = frozenset({"exit", "stopped", "failed"})


def _enabled(directory):
    """Whether the owner's own flag file is there: a plain file in a plain folder, both the owner's."""
    folder, flag = os.lstat(directory), os.lstat(directory / FLAG)
    owner = os.getuid()
    return (stat.S_ISDIR(folder.st_mode) and folder.st_uid == owner and not stat.S_IMODE(folder.st_mode) & 0o077
            and stat.S_ISREG(flag.st_mode) and flag.st_uid == owner and flag.st_nlink == 1)


_RECORD = re.compile(r"(?:claude|codex)-([0-9]{1,20})-[0-9a-f]{8}\.jsonl")


def _room(folder):
    """Make the folder fit under its limits of files and bytes, dropping the oldest records this module wrote
    (O0c P12: nothing else ever removed them). Anything else in the folder is never touched; if it alone fills
    the folder, recording stops."""
    files, size, ours = 0, 0, []
    owner = os.getuid()
    with os.scandir(folder) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            files, size = files + 1, size + info.st_size
            match = _RECORD.fullmatch(entry.name)
            if match and stat.S_ISREG(info.st_mode) and info.st_uid == owner:
                ours.append((int(match.group(1)), entry.name, info.st_size))
    for _, name, length in sorted(ours):
        if files < MAX_FILES and size < MAX_FOLDER_BYTES:
            break
        try:
            os.unlink(folder / name)
        except FileNotFoundError:
            pass
        files, size = files - 1, size - length
    return files < MAX_FILES and size < MAX_FOLDER_BYTES


def recorder(provider, *, requested, effort, tools=()):
    """A recorder for one child run, or None when the owner has not asked for shapes.

    Called before the child is launched: the few file checks here are the only
    ones made until the child has ended.
    """
    directory = _directory
    if directory is None or provider not in {"claude", "codex"}:
        return None
    try:
        if not _enabled(directory):
            return None
        folder = directory / FOLDER
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        kept = os.lstat(folder)
        if (not stat.S_ISDIR(kept.st_mode) or kept.st_uid != os.getuid() or stat.S_IMODE(kept.st_mode) & 0o077
                or not _room(folder)):
            return None
        name = f"{provider}-{time.time_ns()}-{os.urandom(4).hex()}.jsonl"
        return _Recorder(folder / name, {
            "kind": "start", "schema": SCHEMA, "provider": provider,
            "model": requested if type(requested) is str and _MODEL.fullmatch(requested) else "other",
            "effort": _known(effort, _EFFORTS)}, requested, tools)
    except Exception:
        return None


_MODEL = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


def recorded(made, on_frame):
    """`on_frame` preceded by the recorder, which sees every frame before any fence can stop the run."""
    if made is None:
        return on_frame

    def record_then(frame):
        made.frame(frame)
        on_frame(frame)

    return record_then


def finished(made, outcome=None):
    """Close a run's record: the child exited by itself, was stopped, or the run failed before either."""
    if made is not None:
        made.finish("failed" if outcome is None else "exit" if outcome.get("abort") is None else "stopped")
