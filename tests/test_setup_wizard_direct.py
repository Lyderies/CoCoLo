"""T8h: test_setup_sh's wizard terminal tests, each body unchanged, driven in-process.

Every test of WizardTerminalTests except KEPT_TERMINAL_RUNS runs here with the same assertions and
deadlines; wizard_direct_driver replaces only the pty driver and the fake python3's process starts.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_setup_sh import WizardTerminalTests  # noqa: E402
from wizard_direct_driver import DirectDriverMixin  # noqa: E402


class WizardDirectTests(DirectDriverMixin, WizardTerminalTests):
    pass


del WizardTerminalTests  # Collected once, in its own module.

if __name__ == "__main__":
    unittest.main()
