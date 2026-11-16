"""Fixed phase lines a long command reports, so setup can show what it is doing.

Setup asks for them by setting COCOLO_PHASES=1 and shows the latest line, plain
text only, next to the step's elapsed time. Callers pass fixed text built from
counts: never a path, identifier, name or value from the records.

Reporting never changes what the command does (S3-P4): a line that cannot be
written is dropped, and nothing is written where a write could block.
"""

import os
import stat
import sys


def report_phase(text: str) -> None:
    """Write `cocolo-phase: <text>` to standard error when setup asked for phases."""
    if os.environ.get("COCOLO_PHASES") != "1":
        return
    stream = sys.stderr
    if stream is None:
        return
    try:
        try:
            mode = os.fstat(stream.fileno()).st_mode
        except (OSError, ValueError):
            mode = None  # an in-memory stream, which cannot block
        if mode is not None and not stat.S_ISREG(mode):
            return  # a pipe, socket or terminal, where a write can block
        stream.write("cocolo-phase: " + text + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass
