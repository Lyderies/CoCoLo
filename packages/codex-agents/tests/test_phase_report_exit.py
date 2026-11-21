"""S3-P4: a phase line that cannot be written never changes the command's exit status.

The verifier of the COCOLO-run re-review of S3 (2026-11-16) noted that a failed write to
a regular-file standard error could stay in Python's buffer, and the flush at exit would
then end the command with status 120. A file size limit of zero makes every write to the
file fail, on macOS and Linux alike.
"""

import os
import resource
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ORCHESTRATION = Path(__file__).resolve().parents[1] / "native-package" / ".codex" / "orchestration"
CODE = "from codex_agents.phase_report import report_phase; report_phase('upgrading the records')"


def no_file_growth():
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


class PhaseReportExitTests(unittest.TestCase):
    def test_S3_P4_a_failed_write_never_changes_the_exit_status(self):
        with tempfile.TemporaryFile("w+") as stderr:
            done = subprocess.run(
                [sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(ORCHESTRATION)!r}); {CODE}"],
                stderr=stderr, stdout=subprocess.DEVNULL, env={**os.environ, "COCOLO_PHASES": "1"},
                preexec_fn=no_file_growth, timeout=30)
        self.assertEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
