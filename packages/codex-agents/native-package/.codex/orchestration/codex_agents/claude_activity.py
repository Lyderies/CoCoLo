"""Conservative metadata classification for the pinned Claude stream-json path.

Invoke only AFTER the existing init/billing/model/tool fences. Whole assistant
and user frames count as described below. With partial messages enabled, a
streamed text, thinking or tool-input fragment of a message whose start named the
requested model also counts, at most once per PARTIAL_ACTIVITY_SECONDS, so one long
message keeps a working child alive without flooding the monitor. A fragment counts only
inside a content block that was started (once) in the current message, a message whose
stream already started is not counted again, and a fragment a block already carried in
this message is not new work, so replayed partial frames (in any order, with or without
their block starts) do not refresh the timer. A stream that keeps inventing new text is
indistinguishable from work and is bounded by the hard deadline. The frame shapes are assumed from
the client's documented stream-json output and still need a version-pinned
real-client capability/cadence check. No frame, message, tool input, output or
provider timestamp is retained; repeated work is recognised by a short digest of
its message ID and content, so replaying a frame never refreshes the timer.
"""
from __future__ import annotations

import hashlib
import re
import time

MAX_ACTIVITY_ITEMS = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TEXT_BLOCKS = {"text": "text", "thinking": "thinking"}
PARTIAL_ACTIVITY_SECONDS = 1.0
MAX_STREAM_BLOCKS = 256
MAX_STREAM_FRAGMENTS = 65_536  # Distinct fragments remembered per message.
_DELTA_TEXT = {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "partial_json"}


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

    def __init__(self, model, tools, *, clock=time.monotonic):
        self._model = model
        self._tools = frozenset(tools)
        self._clock = clock
        self._streaming = False  # Inside a streamed message from the requested model.
        self._last_partial = None
        self._streamed = set()  # Digests of the message IDs whose stream has started.
        self._blocks = set()  # Open content blocks of the current message.
        self._opened = set()  # Every block index started in the current message.
        self._fragments = set()  # Digests of the fragments already counted in the current message.
        self._started = False
        self._pending = set()
        self._done = set()
        self._work = set()

    def _count(self):
        if len(self._pending) + len(self._done) + len(self._work) + len(self._streamed) >= MAX_ACTIVITY_ITEMS:
            raise ClaudeActivityError("monitor_unavailable")

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
        if kind == "stream_event":
            return self._partial(frame)
        if kind not in {"assistant", "user"}:
            return None  # Init, result, billing and status are not work.
        if not self._started:
            raise ClaudeActivityError()
        message, content = _content(frame)
        if kind == "assistant":
            if frame.get("error") is not None or message.get("model") != self._model:
                return None
            message_id = message.get("id")
            if not _identifier(message_id):
                raise ClaudeActivityError()
            found = False
            for block in content:
                block_type = block.get("type")
                if block_type in _TEXT_BLOCKS:
                    text = block.get(_TEXT_BLOCKS[block_type])
                    if type(text) is not str:
                        raise ClaudeActivityError()
                    if not text.strip():
                        continue
                    work = hashlib.sha256(f"{message_id}\0{block_type}\0{text}".encode()).digest()[:16]
                    if work in self._work:
                        continue  # The same text again is not new work.
                    self._count()
                    self._work.add(work)
                    found = True
                elif block_type == "tool_use":
                    identity = block.get("id")
                    if not _identifier(identity) or block.get("name") not in self._tools:
                        raise ClaudeActivityError()
                    if identity in self._pending or identity in self._done:
                        continue
                    self._count()
                    self._pending.add(identity)
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
                self._done.add(identity)  # A completed call never counts again.
                found = True
        return "activity" if found else None

    def _partial(self, frame):
        """One partial-message event: a fragment of a requested-model message is work, coalesced."""
        if not self._started or frame.get("parent_tool_use_id") is not None:
            raise ClaudeActivityError()
        event = frame.get("event")
        if type(event) is not dict or type(event.get("type")) is not str:
            raise ClaudeActivityError()
        kind = event["type"]
        if kind == "message_start":
            message = event.get("message")
            if type(message) is not dict or not _identifier(message.get("id")):
                raise ClaudeActivityError()
            started = hashlib.sha256(message["id"].encode()).digest()[:16]
            self._blocks, self._opened, self._fragments = set(), set(), set()
            if started in self._streamed:
                # A message whose stream already started is being replayed: none of
                # its fragments is new work.
                self._streaming = False
                return None
            self._count()
            self._streamed.add(started)
            self._streaming = message.get("model") == self._model
            return None
        if kind == "message_stop":
            self._streaming, self._blocks = False, set()
            return None
        if kind in {"content_block_start", "content_block_stop", "content_block_delta"}:
            index = event.get("index")
            if type(index) is not int or not 0 <= index < MAX_STREAM_BLOCKS:
                return None  # A block that cannot be followed is never work.
            if kind == "content_block_start":
                if index not in self._opened:  # A block starts once in its message; a replayed start reopens nothing.
                    self._opened.add(index)
                    self._blocks.add(index)
                return None
            if kind == "content_block_stop":
                self._blocks.discard(index)
                return None
        if kind != "content_block_delta" or not self._streaming or index not in self._blocks:
            return None  # Message deltas, other models and fragments outside an open block are not work.
        delta = event.get("delta")
        if type(delta) is not dict or delta.get("type") not in _DELTA_TEXT:
            return None
        text = delta.get(_DELTA_TEXT[delta["type"]])
        if type(text) is not str:
            raise ClaudeActivityError()
        if not text.strip():
            return None  # Whitespace never refreshes the timer.
        fragment = hashlib.sha256(f"{index}\0{delta['type']}\0{text}".encode()).digest()[:16]
        if fragment in self._fragments or len(self._fragments) >= MAX_STREAM_FRAGMENTS:
            # A fragment this block already carried in this message (a replayed
            # frame, in any order) is not new work; nor is anything past the bound.
            return None
        self._fragments.add(fragment)
        now = self._clock()
        if self._last_partial is not None and now - self._last_partial < PARTIAL_ACTIVITY_SECONDS:
            return None
        self._last_partial = now
        return "activity"
