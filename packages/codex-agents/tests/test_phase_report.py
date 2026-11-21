"""S3-P4: reporting a phase never changes what a command does.

The COCOLO-run security review of S3 (2026-11-16) found that a closed or full standard
error made the phase write raise part-way through an upgrade's backups, and that a full,
undrained pipe could block it. A phase line is dropped on any such failure, and nothing
is written where a write can block.
"""

import errno
import io
import os
import tempfile
import unittest
from unittest import mock

from codex_agents.phase_report import report_phase

ASKED = {"COCOLO_PHASES": "1"}


class FailingStream(io.TextIOBase):
    """An in-memory standard error whose writes fail, as a full disk does."""

    def write(self, text):
        raise OSError(errno.ENOSPC, "no space")


class Adapter(io.TextIOBase):
    """A standard error wrapper whose kind cannot be told: it forwards writes somewhere unknown."""

    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)
        return len(text)


class NoFileno:
    """A standard error replacement with write and flush only."""

    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass


class RaisingFile:
    """A regular file whose write raises an unexpected error."""

    def __init__(self, stream):
        self.stream = stream

    def fileno(self):
        return self.stream.fileno()

    def write(self, text):
        raise RuntimeError("unexpected")

    def flush(self):
        pass


class PhaseReportTests(unittest.TestCase):
    def report(self, stream, environ=ASKED):
        with mock.patch.dict(os.environ, environ, clear=False), mock.patch("sys.stderr", stream):
            report_phase("upgrading the records")

    def test_S3_P3a_a_file_gets_the_fixed_line(self):
        with tempfile.TemporaryFile("w+") as stream:
            self.report(stream)
            stream.seek(0)
            self.assertEqual(stream.read(), "cocolo-phase: upgrading the records\n")

    def test_S3_P3a_nothing_is_written_unless_asked(self):
        with tempfile.TemporaryFile("w+") as stream:
            self.report(stream, {"COCOLO_PHASES": ""})
            stream.seek(0)
            self.assertEqual(stream.read(), "")

    def test_S3_P4_a_closed_standard_error_is_ignored(self):
        stream = io.StringIO()
        stream.close()
        self.report(stream)

    def test_S3_P4_a_failing_standard_error_is_ignored(self):
        self.report(FailingStream())

    def test_S3_P4_no_standard_error_writes_nothing_anywhere(self):
        # The verifier found print(file=None) falls back to standard output, which setup parses.
        with tempfile.TemporaryFile("w+") as stdout, mock.patch("sys.stdout", stdout):
            self.report(None)
            stdout.seek(0)
            self.assertEqual(stdout.read(), "")

    def test_S3_P4_a_stream_of_unknown_kind_gets_nothing(self):
        # The re-review found an adapter passed as in-memory and forwarded to a full pipe.
        for stream in (Adapter(), NoFileno()):
            self.report(stream)
            self.assertEqual(stream.written, [])

    def test_S3_P4_an_in_memory_buffer_gets_the_line(self):
        stream = io.StringIO()
        self.report(stream)
        self.assertEqual(stream.getvalue(), "cocolo-phase: upgrading the records\n")

    def test_S3_P4_any_error_from_the_stream_drops_the_line(self):
        with tempfile.TemporaryFile("w+") as file:
            self.report(RaisingFile(file))

    def test_S3_P4_nothing_is_written_to_a_pipe(self):
        read_end, write_end = os.pipe()
        try:
            os.set_blocking(read_end, False)
            with os.fdopen(write_end, "w", closefd=False) as stream:
                self.report(stream)
            with self.assertRaises(BlockingIOError):
                os.read(read_end, 1)
        finally:
            os.close(read_end)
            os.close(write_end)

    # S3-P7 and P8, from the third COCOLO-run review (2026-11-17).

    def test_S3_P7_an_in_memory_buffer_subclass_gets_nothing(self):
        class Forwarding(io.StringIO):
            def __init__(self):
                super().__init__()
                self.forwarded = []

            def write(self, text):
                self.forwarded.append(text)  # could be forwarding to a full pipe
                return len(text)
        stream = Forwarding()
        self.report(stream)
        self.assertEqual(stream.forwarded, [])

    def test_S3_P8_an_error_from_the_real_write_drops_the_line(self):
        # The write that really runs (to the descriptor) fails, with an ordinary error and with
        # SystemExit: the command carries on either way.
        for error in (OSError(errno.ENOSPC, "no space"), RuntimeError("unexpected"), SystemExit(3)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryFile("w+") as file, \
                    mock.patch("codex_agents.phase_report.os.write", side_effect=error) as write:
                self.report(file)
                self.assertEqual(1, write.call_count)  # The real write was reached.

    def test_S3_P8_a_stream_raising_systemexit_from_fileno_is_dropped(self):
        class Exiting:
            def fileno(self):
                raise SystemExit(3)
        self.report(Exiting())

    def test_S3_P8_a_bad_text_argument_is_dropped(self):
        with mock.patch.dict(os.environ, ASKED, clear=False), tempfile.TemporaryFile("w+") as file, \
                mock.patch("sys.stderr", file):
            report_phase(None)
            report_phase(b"bytes")

    def test_S3_P8_an_interrupt_still_stops_the_command(self):
        with tempfile.TemporaryFile("w+") as file, \
                mock.patch("codex_agents.phase_report.os.write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.report(file)


if __name__ == "__main__":
    unittest.main()
