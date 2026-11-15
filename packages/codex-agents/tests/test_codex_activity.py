from __future__ import annotations

import unittest
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import codex_activity
from codex_agents.codex_activity import CodexActivity, CodexActivityError


class CodexActivityTests(unittest.TestCase):
    def start(self):
        activity = CodexActivity()
        self.assertIsNone(activity.observe({"type": "thread.started", "thread_id": "thread_1"}))
        self.assertEqual("startup", activity.observe({"type": "turn.started"}))
        return activity

    def item(self, kind="item.completed", identity="item_1", **values):
        return {"type": kind, "item": {"id": identity, "type": "reasoning", "text": "PRIVATE-TEXT", **values}}

    def test_only_first_validated_startup_and_nonempty_new_item_stages_count(self):
        activity = self.start()
        self.assertIsNone(activity.observe({"type": "thread.started", "thread_id": "thread_1"}))
        self.assertIsNone(activity.observe({"type": "turn.started"}))
        self.assertIsNone(activity.observe(self.item("item.started", text="")))
        self.assertEqual("activity", activity.observe(self.item("item.updated")))
        self.assertIsNone(activity.observe(self.item("item.updated", text="different content")))
        self.assertEqual("activity", activity.observe(self.item()))
        self.assertIsNone(activity.observe(self.item()))
        self.assertNotIn("PRIVATE", repr(vars(activity)))

    def test_command_output_running_updates_and_metadata_do_not_refresh(self):
        activity = self.start()
        values = {"type": "command_execution", "command": "read-fixture", "status": "in_progress"}
        self.assertEqual("activity", activity.observe(self.item("item.started", **values)))
        self.assertIsNone(activity.observe(self.item("item.updated", aggregated_output="PRIVATE-OUTPUT", **values)))
        values["status"] = "completed"
        self.assertEqual("activity", activity.observe(self.item(**values)))
        for frame in ({"type": "turn.completed", "usage": {}}, {"type": "turn.failed"},
                      self.item(identity="other", type="todo_list"), self.item(identity="other", type="file_change")):
            self.assertIsNone(activity.observe(frame))
        self.assertNotIn("PRIVATE", repr(vars(activity)))
        self.assertNotIn("read-fixture", repr(vars(activity)))

    def test_wrong_thread_missing_identity_bad_shapes_and_type_changes_have_fixed_errors(self):
        for frame in ({"type": []}, {"type": "thread.started", "thread_id": "PRIVATE/INVALID"},
                      {"type": "item.completed", "thread_id": "other"}, self.item(identity=True),
                      self.item(type=[]), self.item(text=[])):
            with self.subTest(frame=frame), self.assertRaisesRegex(CodexActivityError, "^codex_activity_invalid$"):
                self.start().observe(frame)
        activity = self.start()
        activity.observe(self.item())
        with self.assertRaises(CodexActivityError):
            activity.observe(self.item(type="agent_message"))
        with self.assertRaises(CodexActivityError):
            CodexActivity().observe({"type": "turn.started"})

    def test_item_cardinality_overflow_is_monitoring_failure(self):
        activity = self.start()
        with mock.patch.object(codex_activity, "MAX_ACTIVITY_ITEMS", 1):
            activity.observe(self.item())
            with self.assertRaisesRegex(CodexActivityError, "^monitor_unavailable$"):
                activity.observe(self.item(identity="item_2"))


if __name__ == "__main__":
    unittest.main()
