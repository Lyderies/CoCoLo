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


if __name__ == "__main__":
    unittest.main()
