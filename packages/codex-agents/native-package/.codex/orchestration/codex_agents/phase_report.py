"""Fixed phase lines a long command reports, so setup can show what it is doing.

Setup asks for them by setting COCOLO_PHASES=1 and shows the latest line, plain
text only, next to the step's elapsed time. Callers pass fixed text built from
counts: never a path, identifier, name or value from the records.

Reporting never changes what the command does (S3-P4): a line that cannot be
written is dropped, and nothing is written where a write could block.
"""

import io
import os
import stat
import sys


def report_phase(text: str) -> None:
    """Write `cocolo-phase: <text>` to standard error when setup asked for phases."""
    try:
        if os.environ.get("COCOLO_PHASES") != "1":
            return
        stream = sys.stderr
        if stream is None:
            return
        line = "cocolo-phase: " + text + "\n"
        if type(stream) is io.StringIO:
            # Exactly the standard buffer: a subclass may forward anywhere (S3-P7).
            stream.write(line)
            return
        try:
            descriptor = stream.fileno()
            mode = os.fstat(descriptor).st_mode
        except Exception:
            return  # a stream whose kind cannot be told may forward to anything
        if not stat.S_ISREG(mode):
            return  # a pipe, socket or terminal, where a write can block
        # Straight to the file, bypassing the stream's buffer: a failed write leaves nothing
        # for Python's flush at exit to fail on, so the exit status never changes.
        os.write(descriptor, line.encode("ascii", "replace"))
    except KeyboardInterrupt:
        raise  # Ctrl-C still stops the command, exactly as without phases (S3-P8)
    except BaseException:
        pass  # a phase line is never worth changing what the command does (S3-P4, S3-P8)
