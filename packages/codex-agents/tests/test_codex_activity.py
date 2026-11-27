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

    def test_items_in_progress_beyond_the_cap_are_a_monitoring_failure(self):
        activity = self.start()
        with mock.patch.object(codex_activity, "MAX_ACTIVITY_ITEMS", 1):
            activity.observe(self.item("item.started", text="x"))
            with self.assertRaisesRegex(CodexActivityError, "^monitor_unavailable$"):
                activity.observe(self.item("item.started", identity="item_2", text="x"))

    def test_a_long_run_of_completed_items_never_reaches_the_cap(self):
        # O0c M5: only items still in progress are held; a completed item is forgotten.
        activity = self.start()
        with mock.patch.object(codex_activity, "MAX_ACTIVITY_ITEMS", 8):
            for number in range(50):
                identity = f"item_{number}"
                self.assertEqual("activity", activity.observe(self.item("item.started", identity=identity)))
                self.assertEqual("activity", activity.observe(self.item("item.updated", identity=identity)))
                self.assertEqual("activity", activity.observe(self.item(identity=identity)))
            for number in range(50, 100):  # Items seen only as completed.
                self.assertEqual("activity", activity.observe(self.item(identity=f"item_{number}")))
            self.assertLessEqual(len(vars(activity)["_items"]), 8)

    def test_a_late_event_for_a_forgotten_item_is_handled_as_an_unknown_item_never_a_fault(self):
        activity = self.start()
        with mock.patch.object(codex_activity, "MAX_ACTIVITY_ITEMS", 8):
            for number in range(50):
                activity.observe(self.item(identity=f"item_{number}"))
            # item_0 was forgotten long ago: it is a new item to the reader, as any unknown one.
            activity.observe(self.item("item.updated", identity="item_0"))
            activity.observe(self.item(identity="item_0"))
            # The cap still bounds what is in progress.
            for number in range(8):
                activity.observe(self.item("item.started", identity=f"open_{number}", text="x"))
            with self.assertRaisesRegex(CodexActivityError, "^monitor_unavailable$"):
                activity.observe(self.item("item.started", identity="open_8", text="x"))


if __name__ == "__main__":
    unittest.main()
