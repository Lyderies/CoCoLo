"""A long setup step never looks stuck: it shows how long it has run, and says when it can take a while.

The owner (2026-11-15): "the install stage stops, looks blank for ages before it goes
to the next step, this is bad ux". setup.sh's run_cmd shows a spinner and the step's
label; it now also shows the seconds so far and, after a short while, that the step
can take a few minutes.
"""

import re
import subprocess
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.sh"


def function(name):
    """The text of one shell function from setup.sh."""
    text = SETUP.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, name
    return match.group(0)


def run_step(seconds, *, slow_after=None):
    script = "\n".join([
        "C_CYAN=''; C_RESET=''; CMD_N=0; LOGDIR=$(mktemp -d); RUN_QUIET=0",
        "hide_cursor() { :; }; show_cursor() { :; }; log_step() { :; }",
        *([f"RUN_SLOW_SECONDS={slow_after}"] if slow_after is not None else []),
        function("run_cmd"),
        f'run_cmd "Installing" sleep {seconds}',
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, check=True).stdout


class SetupProgressTests(unittest.TestCase):
    def test_a_running_step_shows_how_long_it_has_run(self):
        output = run_step(2.2)
        self.assertIn("Installing", output)
        self.assertRegex(output, r"Installing\s+[12]s")

    def test_after_a_while_it_says_the_step_can_take_a_few_minutes(self):
        output = run_step(2.2, slow_after=1)
        self.assertIn("can take a few minutes", output)
        self.assertNotIn("can take a few minutes", run_step(0.3))


if __name__ == "__main__":
    unittest.main()
