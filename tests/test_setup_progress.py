"""A long setup step never looks stuck: it shows how long it has run, and says when it can take a while.

The owner (2026-11-15): "the install stage stops, looks blank for ages before it goes
to the next step, this is bad ux". setup.sh's run_cmd shows a spinner and the step's
label; it now also shows the seconds so far and, after a short while, that the step
can take a few minutes.
"""

import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.sh"


def function(name):
    """The text of one shell function from setup.sh."""
    text = SETUP.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, name
    return match.group(0)


def run_step(seconds, *, slow_after=None, command=None):
    script = "\n".join([
        "C_CYAN=''; C_RESET=''; CMD_N=0; LOGDIR=$(mktemp -d); RUN_QUIET=0",
        "hide_cursor() { :; }; show_cursor() { :; }; log_step() { :; }",
        *([f"RUN_SLOW_SECONDS={slow_after}"] if slow_after is not None else []),
        function("run_cmd"),
        f'run_cmd "Installing" {command or f"sleep {seconds}"}',
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, check=True).stdout


def emitter(phases):
    """A command that reports each phase on standard error, 1.2 s apart, as the CLI does."""
    code = ("import sys, time\n"
            f"for phase in {phases!r}:\n"
            "    print('cocolo-phase: ' + phase, file=sys.stderr, flush=True)\n"
            "    time.sleep(1.2)\n")
    return f"{sys.executable} -c {shlex.quote(code)}"


class SetupProgressTests(unittest.TestCase):
    def test_a_running_step_shows_how_long_it_has_run(self):
        output = run_step(2.2)
        self.assertIn("Installing", output)
        self.assertRegex(output, r"Installing\s+[12]s")

    def test_after_a_while_it_says_the_step_can_take_a_few_minutes(self):
        output = run_step(2.2, slow_after=1)
        self.assertIn("can take a few minutes", output)
        self.assertNotIn("can take a few minutes", run_step(0.3))

    def test_S3_P1_a_step_shows_the_phase_its_command_reports(self):
        # S3-P1: setup shows the latest `cocolo-phase: <text>` line the command writes to stderr.
        output = run_step(0, command=emitter(["backing up project 2 of 5", "upgrading the records"]))
        self.assertIn("backing up project 2 of 5", output)
        self.assertIn("upgrading the records", output)

    def test_S3_P2_only_plain_phase_text_is_shown(self):
        # S3-P2: letters, digits, spaces and ,.'- only, at most 60 characters; nothing else shown.
        output = run_step(0, command=emitter(["x\x1b[31m$(whoami)/path"]))
        self.assertNotIn("\x1b[31m", output)
        self.assertNotIn("$(whoami)", output)
        self.assertNotIn("/path", output)

    def test_S3_P3c_setup_asks_the_commands_it_runs_for_their_phases(self):
        command = f"{sys.executable} -c " + shlex.quote(
            "import os, sys, time; print('cocolo-phase: ' + ('asked' if os.environ.get('COCOLO_PHASES') == '1' "
            "else 'not asked'), file=sys.stderr, flush=True); time.sleep(1.2)")
        self.assertIn("asked", run_step(0, command=command))
        self.assertNotIn("not asked", run_step(0, command=command))


if __name__ == "__main__":
    unittest.main()
