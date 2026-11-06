from __future__ import annotations

import gc
import io
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import bridge


class WatchdogLifetimeTests(unittest.TestCase):
    def run_child(self, script: str, deadline: float) -> tuple[dict, list]:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            with warnings.catch_warnings(record=True) as observed:
                warnings.simplefilter("always", ResourceWarning)
                result = bridge.run_child(
                    [sys.executable, "-c", script], environment={"PATH": os.defpath},
                    cwd=Path(directory), deadline_seconds=deadline, on_frame=lambda frame: None,
                )
                gc.collect()
        return result, [warning for warning in observed if issubclass(warning.category, ResourceWarning)]

    def test_completed_child_reaps_its_detached_watchdog_without_resource_warning(self):
        result, observed = self.run_child("print('{}', flush=True)", 10)
        self.assertEqual({"returncode": 0, "abort": None}, result)
        self.assertEqual([], [str(warning.message) for warning in observed])

    def test_timed_out_child_reaps_its_detached_watchdog_without_resource_warning(self):
        result, observed = self.run_child("import time; print('{}', flush=True); time.sleep(60)", 0.15)
        self.assertEqual({"returncode": None, "abort": "timed_out"}, result)
        self.assertEqual([], [str(warning.message) for warning in observed])


class WatchdogOwnershipTests(unittest.TestCase):
    def test_interrupted_ack_retains_handle_until_child_cleanup_then_reaps(self):
        child = mock.Mock(pid=424242, stdout=io.BytesIO(), returncode=None)
        helper = mock.Mock(stdout=io.BytesIO())
        tree = mock.Mock(root_birth="fixture-birth")
        tree.identify.return_value = True
        order = []
        helper.wait.side_effect = lambda **kwargs: order.append("watchdog-wait") or 0
        active = {}
        with mock.patch.object(bridge, "tracking_available", return_value=True), \
                mock.patch.object(bridge, "ProcessTree", return_value=tree), \
                mock.patch.object(bridge, "process_table", return_value={}), \
                mock.patch.object(bridge, "_ACTIVE_CHILDREN", active), \
                mock.patch.object(bridge.subprocess, "Popen", return_value=helper), \
                mock.patch.object(bridge, "await_ready", side_effect=KeyboardInterrupt), \
                mock.patch.object(bridge, "_stop_child", side_effect=lambda *_: order.append("child-stop")):
            with self.assertRaises(KeyboardInterrupt):
                bridge.run_child(["fixture"], environment={}, cwd=Path.cwd(), deadline_seconds=10,
                                 on_frame=lambda frame: None, popen=mock.Mock(return_value=child))
        self.assertEqual(["child-stop", "watchdog-wait"], order)
        helper.wait.assert_called_once_with(timeout=bridge.WATCHDOG_REAP_SECONDS)
        helper.kill.assert_not_called()
        helper.terminate.assert_not_called()
        self.assertTrue(helper.stdout.closed)
        self.assertTrue(child.stdout.closed)
        self.assertEqual({}, active)

    def test_final_wait_is_bounded_and_never_signals_an_unfinished_backstop(self):
        for error in (subprocess.TimeoutExpired("fixture", 5), OSError("CANARY-CLEANUP"),
                      RuntimeError("CANARY-CLEANUP")):
            with self.subTest(error=type(error).__name__):
                owner = bridge._WatchdogOwner()
                helper = mock.Mock()
                helper.wait.side_effect = error
                owner._process = helper
                self.assertFalse(owner.reap())
                helper.wait.assert_called_once_with(timeout=5)
                helper.kill.assert_not_called()
                helper.terminate.assert_not_called()

    def test_reap_requires_successful_helper_exit_and_handles_no_launch(self):
        owner = bridge._WatchdogOwner()
        self.assertTrue(owner.reap())
        helper = mock.Mock()
        owner._process = helper
        for code in (0, 1, -9):
            with self.subTest(returncode=code):
                helper.wait.return_value = code
                self.assertEqual(code == 0, owner.reap())

    def test_failed_helper_launch_is_unavailable_without_a_process_to_reap(self):
        owner = bridge._WatchdogOwner()
        with mock.patch.object(bridge.subprocess, "Popen", side_effect=OSError("CANARY-LAUNCH")):
            with self.assertRaises(bridge._Abort) as caught:
                owner.start(424242, "fixture-birth", 100)
        self.assertEqual("watchdog_unavailable", caught.exception.reason)
        self.assertTrue(owner.reap())

    def test_reap_propagates_interrupt_without_signalling_the_backstop(self):
        owner = bridge._WatchdogOwner()
        helper = mock.Mock()
        helper.wait.side_effect = KeyboardInterrupt
        owner._process = helper
        with self.assertRaises(KeyboardInterrupt):
            owner.reap()
        helper.wait.assert_called_once_with(timeout=5)
        helper.kill.assert_not_called()
        helper.terminate.assert_not_called()

    def test_ack_error_survives_a_later_reap_failure_with_bookkeeping_closed(self):
        child = mock.Mock(pid=424242, stdout=io.BytesIO(), returncode=None)
        helper = mock.Mock(stdout=io.BytesIO())
        helper.wait.side_effect = OSError("CANARY-CLEANUP")
        tree = mock.Mock(root_birth="fixture-birth")
        tree.identify.return_value = True
        active = {}
        with mock.patch.object(bridge, "tracking_available", return_value=True), \
                mock.patch.object(bridge, "ProcessTree", return_value=tree), \
                mock.patch.object(bridge, "process_table", return_value={}), \
                mock.patch.object(bridge, "_ACTIVE_CHILDREN", active), \
                mock.patch.object(bridge.subprocess, "Popen", return_value=helper), \
                mock.patch.object(bridge, "await_ready", side_effect=ValueError("fixture-ack-error")), \
                mock.patch.object(bridge, "_stop_child") as stop:
            with self.assertRaisesRegex(ValueError, "^fixture-ack-error$"):
                bridge.run_child(["fixture"], environment={}, cwd=Path.cwd(), deadline_seconds=10,
                                 on_frame=lambda frame: None, popen=mock.Mock(return_value=child))
        stop.assert_called_once_with(child, tree)
        helper.wait.assert_called_once_with(timeout=5)
        helper.kill.assert_not_called()
        self.assertTrue(helper.stdout.closed)
        self.assertTrue(child.stdout.closed)
        self.assertEqual({}, active)

    def test_unconfirmed_watchdog_fences_success_and_preserves_an_earlier_abort(self):
        for raw, expected in ((b"", "watchdog_unavailable"), (b"CANARY-INVALID\n", "unparseable_stream")):
            with self.subTest(raw=raw):
                child = mock.Mock(pid=424242, returncode=0)
                tree = mock.Mock(root_birth="fixture-birth")
                tree.identify.return_value = True
                tree.root_exited.return_value = True
                owner = mock.Mock()
                owner.reap.return_value = False
                selector = mock.MagicMock()
                selector.__enter__.return_value.select.return_value = [(None, None)]
                active = {}
                with mock.patch.object(bridge, "tracking_available", return_value=True), \
                        mock.patch.object(bridge, "ProcessTree", return_value=tree), \
                        mock.patch.object(bridge, "process_table", return_value={}), \
                        mock.patch.object(bridge, "_ACTIVE_CHILDREN", active), \
                        mock.patch.object(bridge, "_WatchdogOwner", return_value=owner), \
                        mock.patch.object(bridge.time, "monotonic", return_value=100), \
                        mock.patch.object(bridge.selectors, "DefaultSelector", return_value=selector), \
                        mock.patch.object(bridge.os, "read", return_value=raw), \
                        mock.patch.object(bridge, "_stop_child") as stop:
                    result = bridge.run_child(
                        ["fixture"], environment={}, cwd=Path.cwd(), deadline_seconds=10,
                        on_frame=lambda frame: None, popen=mock.Mock(return_value=child),
                    )
                self.assertEqual({"returncode": None, "abort": expected}, result)
                stop.assert_called_once_with(child, tree)
                child.stdout.close.assert_called_once()
                owner.reap.assert_called_once()
                self.assertEqual({}, active)
