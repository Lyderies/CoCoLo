"""Tests for the native Codex orchestration package."""

import sys
from pathlib import Path

# The product's import path, for a test module run alone, in any order or in parallel. Only
# the path: the shared environment set-up stays in tests.support, which test modules import
# themselves (a fixture started as `python -m tests.<fixture>` must keep the environment its
# test gave it).
_ORCHESTRATION = Path(__file__).resolve().parent.parent / "native-package" / ".codex" / "orchestration"
if str(_ORCHESTRATION) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATION))
