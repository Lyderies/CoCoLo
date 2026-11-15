"""Terminal fixture waits use elapsed time, independent of wall-clock changes."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_setup_sh as setup_fixtures


@unittest.skipUnless(os.name == "posix", "terminal pipe readiness is POSIX-only")
class WizardClockTests(unittest.TestCase):
    def test_ready_terminal_data_survives_a_forward_wall_clock_jump(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        os.write(write_fd, b"READY")
        terminal = SimpleNamespace(fd=read_fd, screen="", transcript="", fail=self.fail)
        with mock.patch.object(time, "time", side_effect=[1000.0, 1000000.0]):
            setup_fixtures.WizardTerminalTests.expect(terminal, "READY", timeout=.2)
        self.assertEqual("READY", terminal.screen)
        self.assertEqual("READY", terminal.transcript)


if __name__ == "__main__":
    unittest.main()
