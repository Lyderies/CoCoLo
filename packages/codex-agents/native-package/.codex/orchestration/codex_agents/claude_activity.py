"""Conservative metadata classification for the pinned Claude stream-json path.

Invoke only AFTER the existing init/billing/model/tool fences. Only whole
assistant and user frames count (the launch never asks for partial messages),
so the silence cutoff has message granularity. The frame shapes are assumed from
the client's documented stream-json output and still need a version-pinned
real-client capability/cadence check. No frame, message, tool input, output or
provider timestamp is retained.
"""
from __future__ import annotations

import re

MAX_ACTIVITY_ITEMS = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TEXT_BLOCKS = {"text": "text", "thinking": "thinking"}


class ClaudeActivityError(RuntimeError):
    def __init__(self, reason="claude_activity_invalid"):
        self.reason_code = "monitor_unavailable" if reason == "monitor_unavailable" else "claude_activity_invalid"
        super().__init__(self.reason_code)


def _identifier(value):
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _content(frame):
    if frame.get("parent_tool_use_id") is not None:
        raise ClaudeActivityError()  # The read-only tool set has no subagents.
    message = frame.get("message")
    if type(message) is not dict:
        raise ClaudeActivityError()
    content = message.get("content")
    if type(content) is str:
        return message, []
    if type(content) is not list or any(type(block) is not dict for block in content):
        raise ClaudeActivityError()
    return message, content


class ClaudeActivity:
    """Classify validated frames as startup, activity or nothing.

    ``tools`` is the run's allowed tool set; ``model`` the requested model. A
    frame from another model is not work (the caller marks the run rerouted).
    """

    def __init__(self, model, tools):
        self._model = model
        self._tools = frozenset(tools)
        self._started = False
        self._pending = set()
        self._seen = 0

    def _remember(self, identity):
        if self._seen >= MAX_ACTIVITY_ITEMS:
            raise ClaudeActivityError("monitor_unavailable")
        self._seen += 1
        self._pending.add(identity)

    def startup(self):
        if self._started:
            return None
        self._started = True
        return "startup"

    def observe(self, frame):
        if type(frame) is not dict:
            raise ClaudeActivityError()
        kind = frame.get("type")
        if type(kind) is not str:
            raise ClaudeActivityError()
        if kind not in {"assistant", "user"}:
            return None  # Init, result, billing, status and stream metadata are not work.
        if not self._started:
            raise ClaudeActivityError()
        message, content = _content(frame)
        if kind == "assistant":
            if frame.get("error") is not None or message.get("model") != self._model:
                return None
            found = False
            for block in content:
                block_type = block.get("type")
                if block_type in _TEXT_BLOCKS:
                    text = block.get(_TEXT_BLOCKS[block_type])
                    if type(text) is not str:
                        raise ClaudeActivityError()
                    found = found or bool(text.strip())
                elif block_type == "tool_use":
                    identity = block.get("id")
                    if not _identifier(identity) or block.get("name") not in self._tools:
                        raise ClaudeActivityError()
                    if identity in self._pending:
                        continue
                    self._remember(identity)
                    found = True
            return "activity" if found else None
        found = False
        for block in content:
            if block.get("type") != "tool_result":
                continue
            identity = block.get("tool_use_id")
            if not _identifier(identity):
                raise ClaudeActivityError()
            if identity in self._pending:
                self._pending.discard(identity)
                found = True
        return "activity" if found else None
