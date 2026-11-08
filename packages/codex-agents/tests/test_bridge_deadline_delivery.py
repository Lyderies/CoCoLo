"""Original hard deadlines fence buffered delivery; inert Python children only."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import TEMP_ROOT

from codex_agents import bridge


class BufferedDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.monotonic = time.monotonic
        self.offset = 0

    def clock(self):
        return self.monotonic() + self.offset

    def run_fixture(self, code, on_frame):
        return bridge.run_child(
            [sys.executable, "-I", "-B", "-S", "-c", code],
            environment={"PATH": os.defpath, "LANG": "C"}, cwd=self.root,
            deadline_seconds=10, on_frame=on_frame,
        )

    def test_deadline_is_checked_between_frames_from_one_read(self):
        delivered = []

        def accept(frame):
            delivered.append(frame)
            # Validation of the first frame consumes the remaining budget.
            self.offset = 20

        code = 'import sys,time;sys.stdout.write(\'{"n":1}\\n\\n{"n":2}\\n\');sys.stdout.flush();time.sleep(60)'
        with mock.patch.object(bridge.time, "monotonic", side_effect=self.clock):
            outcome = self.run_fixture(code, accept)
        self.assertEqual("timed_out", outcome["abort"])
        self.assertIsNone(outcome["returncode"])
        self.assertEqual([{"n": 1}], delivered)

    def test_eof_after_deadline_cannot_deliver_an_unterminated_frame(self):
        delivered = []
        original_read = os.read

        def read(fd, size):
            value = original_read(fd, size)
            # Only the bridge stream read uses this size; the watchdog's
            # own readiness pipe remains untouched.
            if size == 65_536 and value == b"":
                self.offset = 20
            return value

        with mock.patch.object(bridge.time, "monotonic", side_effect=self.clock), \
                mock.patch.object(bridge.os, "read", side_effect=read):
            outcome = self.run_fixture('import sys;sys.stdout.write(\'{"n":1}\');sys.stdout.flush()', delivered.append)
        self.assertEqual("timed_out", outcome["abort"])
        self.assertIsNone(outcome["returncode"])
        self.assertEqual([], delivered)

    def test_immediate_valid_frames_still_complete_before_the_original_deadline(self):
        delivered = []
        outcome = self.run_fixture('import sys;sys.stdout.write(\'{"n":1}\\n{"n":2}\');sys.stdout.flush()', delivered.append)
        self.assertIsNone(outcome["abort"])
        self.assertEqual(0, outcome["returncode"])
        self.assertEqual([{"n": 1}, {"n": 2}], delivered)


if __name__ == "__main__":
    unittest.main()
