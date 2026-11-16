"""Fixed phase lines a long command reports, so setup can show what it is doing.

Setup asks for them by setting COCOLO_PHASES=1 and shows the latest line, plain
text only, next to the step's elapsed time. Callers pass fixed text built from
counts: never a path, identifier, name or value from the records.
"""

import os
import sys


def report_phase(text: str) -> None:
    """Write `cocolo-phase: <text>` to standard error when setup asked for phases."""
    if os.environ.get("COCOLO_PHASES") == "1":
        print("cocolo-phase: " + text, file=sys.stderr, flush=True)
