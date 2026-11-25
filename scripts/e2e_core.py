"""The shared parts of the lifecycle end-to-end test (PLAN-0.4.0.md T7): the check registry, what a check
sees (Phase), the scenes several checks read, and the constants the checks modules share.

scripts/e2e_lifecycle.py runs the checks; scripts/e2e_checks_<group>.py modules declare them."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # -I leaves the script's own folder off the path

ORCHESTRATION_VERSION = ROOT / "packages" / "codex-agents" / "native-package" / "VERSION"
CHECKS: dict[str, tuple[tuple[str, ...], object]] = {}
# The scene group each check reads: checks of one group share one World, so --jobs runs a group in one process.
CHECK_GROUPS: dict[str, str] = {}
# A reason code: what the report may carry from a run, besides counts and seconds.
# Rough seconds each scene group takes in a run (the longest start first under --jobs; a guess only orders the work).
GROUP_SECONDS = {"default": 20, "fresh": 50, "key": 25, "replace": 35, "observe": 30, "paid": 25,
                 "route_claude": 35, "route_codex": 35, "life_claude": 55, "life_codex": 55, "life_claude_ready": 55,
                 "run_ctl": 90, "run_silence": 40, "run_capacity": 150, "rec_service": 75, "rec_collector": 130,
                 "learn": 110, "guard": 80, "retain": 70, "core_api": 60, "core_records": 60,
                 "upd_all": 340, "owner_load": 90}
REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")


class CheckFailed(AssertionError):
    """A check's expectation did not hold."""


def check(*requirement_ids: str, group: str | None = None):
    """Declare a check and the requirements it proves (read by tests/test_e2e_requirements.py).

    `group` names the scene the check reads (default: the check alone); a run with --jobs keeps one group in one process."""
    def register(function):
        CHECKS[function.__name__] = (requirement_ids, function)
        CHECK_GROUPS[function.__name__] = group or function.__name__
        return function
    return register


class Phase:
    """What one check sees: its isolated environment, and the expectations it records."""

    def __init__(self, name: str, home: Path):
        self.name, self.home, self.expectations, self.reasons = name, home, 0, []

    def expect(self, condition: bool, what: str) -> None:
        """One assertion. `what` is fixed text naming it, never a value from the run."""
        self.expectations += 1
        if not condition:
            raise CheckFailed(what)

    def reason(self, code: str) -> None:
        """Note a reason code a step answered with, for the report."""
        if not REASON.fullmatch(code or ""):
            raise CheckFailed("a step answered with something that is not a reason code")
        self.reasons.append(code)


# Scenes: the runs several checks read. Each is made once per process, on its own isolated World, and
# every World is stopped and removed when the run ends (run_checks).
_SCENES: dict = {}


def close_scenes() -> None:
    for scene in _SCENES.values():
        scene.world.__exit__(None, None, None)
    _SCENES.clear()


# Shared by the L3, L4 and L5 checks.
PROBE = ("provider-observe", "--provider", "claude", "--billing-probe")

ROUTE_TASK = {
    "id": "task-1", "objective": "Read one fixture", "family": "inventory", "complexity": "low", "risk": "low",
    "meaningful": True, "operation": "mechanical", "ambiguity": "known", "context_breadth": "narrow",
    "acceptance_strength": "executable", "routing_evidence_refs": [], "deterministic_validation": True,
    "independent_parts": 1, "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
    "owned_paths": [], "prohibited_actions": ["network"]}
# The route key the task's shape selects in the policy (a bounded, mechanical, known, narrow, executable task).
ROUTE_KEY = "bounded"
CLAUDE_MODEL = "claude-haiku-5-5"  # The only Claude model this fake project allows (answers: claude_models).
