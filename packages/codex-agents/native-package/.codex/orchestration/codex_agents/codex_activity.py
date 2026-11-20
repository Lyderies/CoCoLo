"""Conservative metadata classification for the pinned Codex exec JSON path.

Invoke only AFTER the existing model/tool/event fences. The dedicated retained
execution supplies attribution; labels alone do not prove native identity. This
candidate still needs a version-pinned real-client capability/cadence check.
No frame, message, command, reasoning, output or provider timestamp is retained.
"""
from __future__ import annotations

from collections import OrderedDict
import re

# Items still in progress, per run. A completed item is forgotten at once (it moves to a
# recent window of the same size, so an immediate replay is still recognised; the oldest
# entry of the window is dropped, never reported), so a long healthy run never reaches
# this. Only more items open at once than this is a monitoring failure.
MAX_ACTIVITY_ITEMS = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_STAGES = {"item.started": 1, "item.updated": 2, "item.completed": 3}


class CodexActivityError(RuntimeError):
    def __init__(self, reason="codex_activity_invalid"):
        self.reason_code = "monitor_unavailable" if reason == "monitor_unavailable" else "codex_activity_invalid"
        super().__init__(self.reason_code)


def _identifier(value):
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


class CodexActivity:
    def __init__(self):
        self._thread_id = None
        self._started = False
        self._items = {}  # In progress: id -> (type, stage).
        self._finished = OrderedDict()  # Recently completed: id -> type; the oldest is forgotten.

    def observe(self, frame):
        if type(frame) is not dict:
            raise CodexActivityError()
        kind = frame.get("type")
        if type(kind) is not str:
            raise CodexActivityError()
        if kind == "thread.started":
            thread = frame.get("thread_id")
            if not _identifier(thread) or (self._thread_id is not None and thread != self._thread_id):
                raise CodexActivityError()
            self._thread_id = thread
            return None
        if "thread_id" in frame and (self._thread_id is None or frame["thread_id"] != self._thread_id):
            raise CodexActivityError()
        if kind == "turn.started":
            if self._thread_id is None:
                raise CodexActivityError()
            if self._started:
                return None
            self._started = True
            return "startup"
        if kind not in _STAGES:
            return None  # Completion/error/billing/metadata are not work.
        item = frame.get("item")
        if type(item) is not dict:
            raise CodexActivityError()
        item_type = item.get("type")
        if type(item_type) is not str:
            raise CodexActivityError()
        if item_type not in {"agent_message", "reasoning", "command_execution"}:
            return None
        identity = item.get("id")
        if not self._started or not _identifier(identity):
            raise CodexActivityError()
        stage = _STAGES[kind]
        previous = self._items.get(identity)
        if previous is None and identity in self._finished:
            previous = (self._finished[identity], _STAGES["item.completed"])
        if previous is not None:
            if previous[0] != item_type:
                raise CodexActivityError()
            if stage <= previous[1]:
                return None
        elif stage < _STAGES["item.completed"] and len(self._items) >= MAX_ACTIVITY_ITEMS:
            raise CodexActivityError("monitor_unavailable")
        if stage < _STAGES["item.completed"]:
            self._items[identity] = (item_type, stage)
        else:
            self._items.pop(identity, None)
            self._finished[identity] = item_type
            self._finished.move_to_end(identity)
            while len(self._finished) > MAX_ACTIVITY_ITEMS:
                self._finished.popitem(last=False)
        if item_type in {"agent_message", "reasoning"}:
            text = item.get("text")
            if type(text) is not str:
                if kind == "item.started":
                    return None
                raise CodexActivityError()
            return "activity" if text.strip() else None
        command, status = item.get("command"), item.get("status")
        if type(command) is not str or not command.strip() or type(status) is not str:
            raise CodexActivityError()
        if kind == "item.started" and status == "in_progress":
            return "activity"
        if kind == "item.completed" and status in {"completed", "failed"}:
            return "activity"
        if kind == "item.updated" and status == "in_progress":
            return None  # Output/unchanged running status cannot refresh forever.
        raise CodexActivityError()
