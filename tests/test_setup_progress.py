"""A long setup step never looks stuck: it shows how long it has run, and says when it can take a while.

The owner (2026-11-15): "the install stage stops, looks blank for ages before it goes
to the next step, this is bad ux". setup.sh's run_cmd shows a spinner and the step's
label; it now also shows the seconds so far and, after a short while, that the step
can take a few minutes.
"""

import fcntl
import os
import pty
import re
import shlex
import struct
import subprocess
import sys
import termios
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.sh"


def function(name):
    """The text of one shell function from setup.sh."""
    text = SETUP.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, name
    return match.group(0)


def run_step(seconds, *, slow_after=None, command=None, columns=None, both=False, locale=None, lang=None,
             prelude=(), status=False):
    script = "\n".join([
        *([f"export LC_ALL={locale}"] if locale else []),
        *([f"unset LC_ALL; export LANG={lang}"] if lang else []),
        *prelude,
        "C_CYAN=''; C_RESET=''; CMD_N=0; LOGDIR=$(mktemp -d); RUN_QUIET=0",
        *([f"COLUMNS={columns}"] if columns is not None else []),
        "hide_cursor() { :; }; show_cursor() { :; }; log_step() { :; }",
        f"RUN_SLOW_SECONDS={10 if slow_after is None else slow_after}",  # setup.sh's own default is 10
        function("terminal_width"),
        function("run_cmd"),
        f'run_cmd "Installing" {command or f"sleep {seconds}"}; echo "status=$?"',
    ])
    # Never the standard input of whatever runs the tests, which may be a terminal (S3-P9c).
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, check=True,
                          stdin=subprocess.DEVNULL)
    if status:
        return done.stdout, done.stderr
    stdout = done.stdout.replace("status=0\n", "")
    return (stdout, done.stderr) if both else stdout


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

    def test_S3_P5_the_progress_line_never_wraps(self):
        # S3-P5: the review found a 60-character phase made an 81-column line on an 80-column terminal.
        output = run_step(2.2, slow_after=1, columns=40, command=emitter(["p" * 60, "q" * 60]))
        # Text mode reads each carriage return as a line break, so every frame is one line.
        frames = [frame.replace("\x1b[K", "") for frame in output.splitlines() if frame.strip()]
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 40 for frame in frames), max(map(len, frames)))

    def test_S3_P6_only_the_end_of_the_error_log_is_read(self):
        # S3-P6: setup reads the last 4 KiB on each redraw, so an earlier phase behind 8 KiB of
        # other output is no longer found.
        code = ("import sys, time\n"
                "print('cocolo-phase: early phase', file=sys.stderr)\n"
                "print('x' * 8192, file=sys.stderr, flush=True)\n"
                "time.sleep(1.2)\n")
        output = run_step(0, command=f"{sys.executable} -c {shlex.quote(code)}")
        self.assertNotIn("early phase", output)

    def test_S3_P2_a_nul_byte_in_a_phase_prints_nothing_else(self):
        # The verifier found bash warns "ignored null byte" on setup's own screen for each redraw.
        code = ("import sys, time\n"
                "sys.stderr.write('cocolo-phase: before\\0after\\n'); sys.stderr.flush()\n"
                "time.sleep(1.2)\n")
        output, errors = run_step(0, command=f"{sys.executable} -c {shlex.quote(code)}", both=True)
        self.assertNotIn("null byte", output + errors)
        self.assertIn("beforeafter", output)

    def test_S3_P2_only_ascii_letters_whatever_the_locale(self):
        output = run_step(0, command=emitter(["caf\u00e9 ok"]), locale="en_US.UTF-8")
        self.assertIn("caf ok", output)
        self.assertNotIn("\u00e9", output)

    def test_S3_P2_no_byte_makes_setup_print_a_warning(self):
        # The re-review found tr warned on invalid UTF-8 and on a character cut at the 4 KiB edge.
        code = ("import sys, time\n"
                "out = sys.stderr.buffer\n"
                "out.write('\u00e9'.encode() + b'x' * 4094 + b'\\ncocolo-phase: \\xff valid\\n'); out.flush()\n"
                "time.sleep(1.2)\n")
        output, errors = run_step(0, command=f"{sys.executable} -c {shlex.quote(code)}", both=True,
                                  lang="en_US.UTF-8")
        self.assertEqual(errors, "")
        self.assertIn("valid", output)

    def test_S3_P5_any_width_value_lets_the_step_finish(self):
        # The re-review found COLUMNS=08 (invalid octal) left run_cmd before it waited for the command.
        for columns in ("08", "abc", "00080", "4", "0"):
            stdout, errors = run_step(1.2, columns=columns, status=True)
            self.assertIn("status=0", stdout, columns)
            self.assertEqual(errors, "", columns)
            frames = [frame.replace("\x1b[K", "") for frame in stdout.splitlines()[:-1] if frame.strip()]
            width = int(columns, 10) if columns.isdigit() else 80
            self.assertTrue(all(len(frame) < max(width, 1) for frame in frames), (columns, frames[:2]))

    def test_S3_P5_a_step_returns_its_exit_status(self):
        stdout, _ = run_step(0, columns="08", command="sh -c 'sleep 0.5; exit 3'", status=True)
        self.assertIn("status=3", stdout)

    def test_S3_P5_the_width_comes_from_the_terminal_itself(self):
        # The verifier noted tput inside $( ) may answer 80 from the pipe. Setup's own terminal, here
        # a pseudo-terminal 30 columns wide on its standard input, decides the width.
        primary, secondary = pty.openpty()
        try:
            fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 30, 0, 0))
            script = "\n".join([
                "unset COLUMNS",
                "C_CYAN=''; C_RESET=''; CMD_N=0; LOGDIR=$(mktemp -d); RUN_QUIET=0; RUN_SLOW_SECONDS=10",
                "hide_cursor() { :; }; show_cursor() { :; }; log_step() { :; }",
                function("terminal_width"), function("run_cmd"),
                f'run_cmd "Installing" {emitter(["p" * 60])}',
            ])
            environ = {key: value for key, value in os.environ.items() if key != "COLUMNS"}
            done = subprocess.run(["bash", "-c", script], stdin=secondary, capture_output=True, text=True,
                                  timeout=30, check=True, env=environ)
        finally:
            os.close(primary)
            os.close(secondary)
        frames = [frame.replace("\x1b[K", "") for frame in done.stdout.splitlines() if frame.strip()]
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 30 for frame in frames), max(map(len, frames)))

    def test_S3_P5_a_resized_terminal_is_followed(self):
        # tput answers 80 for the first redraws, then 30, as a terminal resized during a step.
        prelude = ['TPUT_N=$(mktemp)',
                   'tput() { local n; n=$(($(wc -c <"$TPUT_N") + 1)); printf x >>"$TPUT_N"; '
                   'if [ "$n" -le 3 ]; then echo 80; else echo 30; fi; }']
        output = run_step(0, prelude=prelude, command=emitter(["p" * 60, "q" * 60]))
        frames = [frame.replace("\x1b[K", "") for frame in output.splitlines() if frame.strip()]
        self.assertTrue(any(len(frame) > 30 for frame in frames[:3]), frames[:3])
        self.assertTrue(all(len(frame) < 30 for frame in frames[-3:]), frames[-3:])

    # S3-P9, from the third COCOLO-run review and its verifier (2026-11-17).

    def test_S3_P9a_a_terminal_reporting_size_0_still_gets_a_frame(self):
        # stty answers "0 0" (a terminal that does not know its size): tput's 50 is used instead.
        prelude = ['stty() { echo "0 0"; }', 'tput() { echo 50; }']
        output = run_step(1.2, prelude=prelude, command=emitter(["p" * 60]))
        frames = [frame.replace("\x1b[K", "") for frame in output.splitlines() if frame.strip()]
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 50 for frame in frames), max(map(len, frames)))
        self.assertTrue(any(len(frame) > 40 for frame in frames))

    def test_S3_P9a_tput_answering_0_falls_back_to_80_columns(self):
        # Neither stty nor tput knows the size: setup draws an 80-column frame, not none.
        prelude = ['stty() { echo "0 0"; }', 'tput() { echo 0; }']
        output = run_step(1.2, prelude=prelude, command=emitter(["p" * 120]))
        frames = [frame.replace("\x1b[K", "") for frame in output.splitlines() if frame.strip()]
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 80 for frame in frames), max(map(len, frames)))
        self.assertTrue(any(len(frame) > 70 for frame in frames), max(map(len, frames)))

    def test_S3_P9a_tput_failing_falls_back_to_80_columns(self):
        prelude = ['stty() { echo "0 0"; }', 'tput() { return 1; }']
        output = run_step(1.2, prelude=prelude, command=emitter(["p" * 120]))
        frames = [frame.replace("\x1b[K", "") for frame in output.splitlines() if frame.strip()]
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 80 for frame in frames), max(map(len, frames)))
        self.assertTrue(any(len(frame) > 70 for frame in frames), max(map(len, frames)))

    def test_S3_P9b_each_redraw_erases_the_last_frame_even_when_too_narrow(self):
        # A frame is drawn at 80 columns; then the terminal becomes 4 columns wide, too narrow for
        # a frame: each redraw still erases the line, so the earlier frame does not stay.
        # The size file is the test's own, removed when it ends (fixture fix from the fourth review).
        handle, size = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(os.unlink, size)
        prelude = [f'SIZE={shlex.quote(size)}; echo "24 80" >"$SIZE"', 'stty() { cat "$SIZE"; }']
        command = "sh -c 'sleep 0.6; echo \"24 4\" >\"$SIZE\"; sleep 0.9'"
        output = run_step(0, prelude=["export SIZE", *prelude], command=command)
        last_frame = output.rindex("Installing")
        # Text mode reads each carriage return as a line break, so count the erase codes alone.
        self.assertGreaterEqual(output[last_frame:].count("\x1b[K"), 3)


if __name__ == "__main__":
    unittest.main()
