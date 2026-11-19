"""S5 W11 and W12.1: the Python wizard's command runner keeps S3-P1 to P9 (tests/test_setup_progress.py).

Each case below asserts what its setup.sh counterpart asserts, on the Python runner. The runner
starts the command, so this is an integration test.
"""

import io
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from setup_wizard import terminal  # noqa: E402


def emitter(phases):
    """A command that reports each phase on standard error, 1.2 s apart, as the CLI does."""
    code = ("import sys, time\n"
            f"for phase in {phases!r}:\n"
            "    print('cocolo-phase: ' + phase, file=sys.stderr, flush=True)\n"
            "    time.sleep(1.2)\n")
    return [sys.executable, "-c", code]


def python(code):
    return [sys.executable, "-c", code]


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.logdir = Path(directory.name)

    def run_step(self, argv, *, slow_after=10, width=lambda: 80, quiet=False):
        out = io.StringIO()
        term = terminal.Terminal(out=out, read=lambda count, timeout: b"", colours=terminal.NO_COLOURS,
                                 size=lambda: (80, 24))
        status = term.run_cmd("Installing", argv, logdir=self.logdir, slow_seconds=slow_after, width=width,
                              quiet=quiet)
        return out.getvalue(), status, term

    @staticmethod
    def frames(output):
        # The cursor show and hide codes are not frames (setup.sh's tests stubbed them out).
        output = output.replace("\x1b[?25l", "").replace("\x1b[?25h", "")
        return [frame.replace("\x1b[K", "") for frame in output.split("\r") if frame.replace("\x1b[K", "").strip()]

    def test_a_running_step_shows_how_long_it_has_run(self):
        output, _, _ = self.run_step(["sleep", "2.2"])
        self.assertRegex(output, r"Installing\s+[12]s")

    def test_after_a_while_it_says_the_step_can_take_a_few_minutes(self):
        self.assertIn("can take a few minutes", self.run_step(["sleep", "2.2"], slow_after=1)[0])
        self.assertNotIn("can take a few minutes", self.run_step(["sleep", "0.3"])[0])

    def test_S3_P1_a_step_shows_the_phase_its_command_reports(self):
        output, _, _ = self.run_step(emitter(["backing up project 2 of 5", "upgrading the records"]))
        self.assertIn("backing up project 2 of 5", output)
        self.assertIn("upgrading the records", output)

    def test_S3_P2_only_plain_phase_text_is_shown(self):
        output, _, _ = self.run_step(emitter(["x\x1b[31m$(whoami)/path"]))
        self.assertNotIn("\x1b[31m", output)
        self.assertNotIn("$(whoami)", output)
        self.assertNotIn("/path", output)

    def test_S3_P2_only_ascii_letters_and_no_nul(self):
        output, _, _ = self.run_step(emitter(["caf\u00e9 ok"]))
        self.assertIn("caf ok", output)
        self.assertNotIn("\u00e9", output)
        output, _, _ = self.run_step(python("import sys, time\nsys.stderr.write('cocolo-phase: before\\0after\\n')\n"
                                            "sys.stderr.flush(); time.sleep(1.2)\n"))
        self.assertIn("beforeafter", output)
        output, _, _ = self.run_step(python("import sys, time\nout = sys.stderr.buffer\n"
                                            "out.write(b'\\xc3' + b'x' * 4094 + b'\\ncocolo-phase: \\xff valid\\n')\n"
                                            "out.flush(); time.sleep(1.2)\n"))
        self.assertIn("valid", output)

    def test_S3_P3c_setup_asks_the_commands_it_runs_for_their_phases(self):
        code = ("import os, sys, time; print('cocolo-phase: ' + ('asked' if os.environ.get('COCOLO_PHASES') == '1' "
                "else 'not asked'), file=sys.stderr, flush=True); time.sleep(1.2)")
        output, _, _ = self.run_step(python(code))
        self.assertIn("asked", output)
        self.assertNotIn("not asked", output)

    def test_S3_P5_the_progress_line_never_wraps(self):
        output, _, _ = self.run_step(emitter(["p" * 60, "q" * 60]), slow_after=1, width=lambda: 40)
        frames = self.frames(output)
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 40 for frame in frames), max(map(len, frames)))

    def test_S3_P5_a_resized_terminal_is_followed(self):
        answers = iter([80, 80, 80])
        output, _, _ = self.run_step(emitter(["p" * 60, "q" * 60]), width=lambda: next(answers, 30))
        frames = self.frames(output)
        self.assertTrue(any(len(frame) > 30 for frame in frames[:3]), frames[:3])
        self.assertTrue(all(len(frame) < 30 for frame in frames[-3:]), frames[-3:])

    def test_S3_P6_only_the_end_of_the_error_log_is_read(self):
        code = ("import sys, time\nprint('cocolo-phase: early phase', file=sys.stderr)\n"
                "print('x' * 8192, file=sys.stderr, flush=True)\ntime.sleep(1.2)\n")
        self.assertNotIn("early phase", self.run_step(python(code))[0])

    def test_S3_P5_a_step_returns_its_exit_status(self):
        self.assertEqual(3, self.run_step(["sh", "-c", "sleep 0.5; exit 3"])[1])
        self.assertEqual(0, self.run_step(["true"])[1])

    def test_S3_P9b_each_redraw_erases_the_last_frame_even_when_too_narrow(self):
        started = time.monotonic()
        output, _, _ = self.run_step(["sleep", "1.5"], width=lambda: 80 if time.monotonic() - started < 0.6 else 4)
        last_frame = output.rindex("Installing")
        self.assertGreaterEqual(output[last_frame:].count("\x1b[K"), 3)

    def test_the_step_log_says_how_each_command_ended_never_its_output(self):
        """W9.5: one line per command for the ? screen."""
        _, _, term = self.run_step(python("print('SECRET output'); raise SystemExit(2)"))
        self.assertRegex(term.step_log[-1], r"^\d\d:\d\d:\d\d  Installing: stopped, exit 2$")
        _, _, term = self.run_step(["true"])
        self.assertTrue(term.step_log[-1].endswith("  Installing: done"))

    def test_output_stays_in_the_private_logs_or_is_discarded_when_quiet(self):
        _, _, term = self.run_step(python("print('kept'); import sys; print('err', file=sys.stderr)"))
        self.assertEqual("kept\n", Path(term.last_out).read_text())
        self.assertEqual(0o600, Path(term.last_out).stat().st_mode & 0o777)
        _, _, term = self.run_step(python("print('identity')"), quiet=True)
        self.assertIsNone(term.last_out)
        self.assertNotIn("identity", "".join(p.read_text() for p in self.logdir.iterdir()))

    def test_W12_1_stopping_ends_the_whole_process_group(self):
        """SIGTERM during a slow command leaves no child running."""
        marker = self.logdir / "child-pid"
        code = ("import subprocess, sys, time\n"
                f"child = subprocess.Popen(['sleep', '30']); open({str(marker)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(30)\n")
        out = io.StringIO()
        term = terminal.Terminal(out=out, read=lambda count, timeout: b"", colours=terminal.NO_COLOURS,
                                 size=lambda: (80, 24))

        def stop_soon(signum, frame):
            raise terminal.Stop(130)
        previous = signal.signal(signal.SIGALRM, stop_soon)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, 1.5)
        with self.assertRaises(terminal.Stop):
            term.run_cmd("Installing", python(code), logdir=self.logdir, slow_seconds=10, width=lambda: 80)
        child = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail(f"child {child} still running: {shlex.quote(str(child))}")


    def test_the_command_starts_before_the_screen_is_drawn(self):
        """A screen redraw that blocks (a terminal not being read) never holds the command back."""
        marker = self.logdir / "started"
        seen = []
        out = io.StringIO()
        term = terminal.Terminal(out=out, read=lambda count, timeout: b"", colours=terminal.NO_COLOURS,
                                 size=lambda: (80, 24))
        term.run_cmd("Installing", python(f"open({str(marker)!r}, 'w').close()\nimport time; time.sleep(0.5)"),
                     logdir=self.logdir, slow_seconds=10, width=lambda: 80,
                     draw=lambda: seen.append(self.wait_for(marker)))
        self.assertEqual([True], seen)

    @staticmethod
    def wait_for(path, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.02)
        return False


    def test_S3_P5_any_width_value_lets_the_step_finish(self):
        for columns in (0, 4, 8, 80):
            with self.subTest(columns=columns):
                output, status, _ = self.run_step(["sleep", "0.5"], width=lambda c=columns: c)
                self.assertEqual(0, status)
                self.assertTrue(all(len(frame) < max(columns, 1) for frame in self.frames(output)), columns)


class WidthTests(unittest.TestCase):
    """S3-P9a and S3-P5 on terminal_width itself: (COLUMNS, the terminal's size, the terminfo answer)."""

    def test_terminal_width_for_every_size_answer(self):
        for stty, tput, columns, expected in (
            ("0 0", (True, "0"), None, 80),
            ("0 0", (True, "00"), None, 80),
            ("0 0", (False, ""), None, 80),
            ("0 0", (True, ""), None, 80),
            ("0 0", (False, "42"), None, 80),  # A failing answer is not trusted, whatever it says.
            ("0 0", (True, "100"), None, 100),
            ("24 120", (True, "100"), None, 120),
            ("24 120", (True, "100"), "0", 0),
            ("24 120", (True, "100"), "08", 8),
            (None, (True, "50"), None, 50),
            ("24 120", (True, "1"), "abc", 80),
            ("24 120", (True, "1"), "00080", 80),
            ("24 120", (True, "1"), "123456", 9999),
        ):
            with self.subTest(stty=stty, tput=tput, columns=columns):
                self.assertEqual(expected, terminal.terminal_width(columns, stty, tput))



class LiveWidthTests(unittest.TestCase):
    def test_S3_P5_the_width_comes_from_the_terminal_itself(self):
        """The wizard's own terminal (standard input), here a pseudo-terminal 30 columns wide, decides."""
        import fcntl
        import pty
        import struct
        import termios
        primary, secondary = pty.openpty()
        try:
            fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 30, 0, 0))
            environ = {key: value for key, value in os.environ.items() if key != "COLUMNS"}
            with unittest.mock.patch.dict(os.environ, environ, clear=True):
                self.assertEqual(30, terminal.live_width(secondary))
        finally:
            os.close(primary)
            os.close(secondary)


if __name__ == "__main__":
    unittest.main()
