"""HM-0: the health monitor's pass runner, finding record and status view.

The owner decided a health monitor as code (PLAN-0.4.0.md, "Health monitor"; no model use): a
reconcile loop in the collector's sweep. Each pass runs a list of checks. A check compares what
should be with what is, fixes only what is safe to fix, and returns findings; everything it does not
fix, and every fix that failed, is a `reported` finding that reaches the status line and the owner.
Later slices (HM-1 to HM-6) each add one check; this module is the frame they plug into.

- A pass is bounded in time (`PASS_BUDGET_SECONDS`) and in checks (`MAX_CHECKS_PER_PASS`). A check
  starts only when a whole time share (`CHECK_SHARE_SECONDS`) is left, and runs on its own thread so
  one that hangs is cut at its share and reported; it is never started again while it still runs.
  Checks the pass had no room for are reported once, and come first at the next pass.
- A check that raises or returns something invalid becomes a finding of its own that names the
  exception's type only, never its message. Neither stops the pass.
- The pass takes no ledger lock: it runs after the sweep cycle released it, on the sweep thread, and
  a check that needs the ledger asks for it as any other reader does, with the lock's own time limit.
- A finding is a closed record: check, scope (machine or one project), a subject that is a name or a
  digest, fixed or reported, a reason code and one plain sentence. A project's finding is shown only
  to that project; machine findings to every project.
"""

from __future__ import annotations

import collections
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# One pass: at most this long, this many checks, and a check at most this long (a whole share must
# be left to start one). The pass runs on the sweep thread, so it also bounds how long a pass can
# delay the next batch of account hold releases.
PASS_BUDGET_SECONDS = 2.0
CHECK_SHARE_SECONDS = 1.0
MAX_CHECKS_PER_PASS = 16
# At most this many checks are registered in all.
MAX_REGISTERED_CHECKS = 64
# One check's findings kept per pass (more are cut and said so); the status shows this many at most.
MAX_FINDINGS_PER_CHECK = 16
VIEW_MAX_FINDINGS = 32
# Fixed findings stay in the status line after the pass that fixed them: the newest of these.
RECENT_FIXED = 16
# The self-check calls a pass late when it comes this many sweep intervals after the last one.
LATE_PASS_INTERVALS = 3

# The name the monitor reports under for what is not one check's (a pass without room for every check).
MONITOR_NAME = "health_monitor"
CHECK_NAME = re.compile(r"[a-z][a-z0-9_]{1,47}")
SUBJECT = re.compile(r"[A-Za-z0-9_.:@+-]{1,128}")
REASON_CODE = re.compile(r"[a-z][a-z_]{2,63}")
PROJECT_ID = re.compile(r"prj_[0-9a-f]{32}")
MAX_TEXT = 240
SCOPES = ("machine", "project")
OUTCOMES = ("fixed", "reported")
SUMMARY_MAX = 200


def _matches(pattern: re.Pattern, value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class Finding:
    """One thing a check found. Closed and content-free: the subject is a name or digest (no spaces, no
    slashes), the text one plain sentence; nothing else is stored or shown."""

    check: str
    scope: str
    subject: str
    outcome: str
    reason_code: str
    text: str
    project_id: str | None = None

    def __post_init__(self) -> None:
        if not _matches(CHECK_NAME, self.check):
            raise ValueError("finding check")
        if self.scope not in SCOPES or self.outcome not in OUTCOMES:
            raise ValueError("finding scope or outcome")
        if not _matches(SUBJECT, self.subject) or not _matches(REASON_CODE, self.reason_code):
            raise ValueError("finding subject or reason")
        if (type(self.text) is not str or not 1 <= len(self.text) <= MAX_TEXT
                or not self.text.isprintable()):
            raise ValueError("finding text")
        if (self.scope == "project") != (self.project_id is not None) or (
                self.project_id is not None and not _matches(PROJECT_ID, self.project_id)):
            raise ValueError("finding project")

    def shown(self) -> dict:
        """The form a status line carries: never the project's id (the viewer is that project)."""
        return {"check": self.check, "scope": self.scope, "subject": self.subject, "outcome": self.outcome,
                "reason_code": self.reason_code, "text": self.text}

    def visible_to(self, project_id: str | None) -> bool:
        return self.scope == "machine" or (project_id is not None and self.project_id == project_id)


@dataclass(frozen=True)
class CheckContext:
    """What a check is given: when its share ends (a monotonic time; stop early when it passes), the
    wall clock, and how long since the last pass began (monotonic, so a machine that slept is not late)."""

    deadline: float
    now_ms: int
    since_last_pass_ms: int | None

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


class Check:
    """A check: a unique `name` (lowercase words with underscores) and `run(context) -> findings`. It
    returns only findings that carry its own name."""

    name = "unnamed"

    def run(self, context: CheckContext) -> Sequence[Finding]:  # pragma: no cover - interface
        raise NotImplementedError


class SelfCheck(Check):
    """The one built-in check: the pass itself runs on time. A pass much later than the sweep interval
    means the sweep thread was held up (a stuck sweep step), which no other check could report."""

    name = "self_check"

    def __init__(self, sweep_interval_seconds: float) -> None:
        self.interval_ms = int(sweep_interval_seconds * 1000)

    def run(self, context: CheckContext) -> list[Finding]:
        gap = context.since_last_pass_ms
        if gap is None or gap <= LATE_PASS_INTERVALS * self.interval_ms:
            return []
        return [Finding(self.name, "machine", "pass", "reported", "pass_late",
                        f"The health pass came {gap // 1000} s after the last one, not every "
                        f"{self.interval_ms // 1000} s: the collector's sweep was held up.")]


@dataclass(frozen=True)
class _PassReport:
    started_at_ms: int
    duration_ms: int
    attempted: int
    states: tuple  # (check name, state) for every registered check
    findings: tuple  # this pass's findings, fixed and reported


class HealthMonitor:
    def __init__(self, checks: Sequence[Check] = (), *, pass_budget_seconds: float = PASS_BUDGET_SECONDS,
                 check_share_seconds: float = CHECK_SHARE_SECONDS, max_checks: int = MAX_CHECKS_PER_PASS) -> None:
        self.pass_budget_seconds = pass_budget_seconds
        self.check_share_seconds = check_share_seconds
        self.max_checks = max_checks
        self._checks: list[Check] = []
        self._next = 0  # where the next pass starts, so checks left out come first
        self._threads: dict[str, threading.Thread] = {}
        self._pass_lock = threading.Lock()  # one pass at a time; never a ledger lock
        self._publish = threading.Lock()
        self._last: _PassReport | None = None
        self._last_mono_ms: int | None = None
        self._fixed: collections.deque = collections.deque(maxlen=RECENT_FIXED)
        for check in checks:
            self.register(check)

    @classmethod
    def with_builtin_checks(cls, *, sweep_interval_seconds: float, **limits) -> HealthMonitor:
        return cls((SelfCheck(sweep_interval_seconds),), **limits)

    def register(self, check: Check) -> None:
        name = getattr(check, "name", None)
        if not _matches(CHECK_NAME, name) or name == MONITOR_NAME:
            raise ValueError("a check's name is lowercase words with underscores")
        if any(known.name == name for known in self._checks) or not callable(getattr(check, "run", None)):
            raise ValueError("a check is registered once, with a run method")
        if len(self._checks) >= MAX_REGISTERED_CHECKS:
            raise ValueError("too many checks")
        self._checks.append(check)

    def run_pass(self, *, now_ms: int | None = None, mono_ms: int | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
        """One bounded pass. Nothing a check does can raise out of here."""
        if not self._pass_lock.acquire(blocking=False):
            return  # a pass is running (the sweep thread is the only caller); never two at once
        try:
            self._pass(now_ms, mono_ms, should_stop)
        finally:
            self._pass_lock.release()

    def _pass(self, now_ms, mono_ms, should_stop) -> None:
        started = time.monotonic()
        deadline = started + self.pass_budget_seconds
        now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        mono_ms = int(started * 1000) if mono_ms is None else mono_ms
        since = None if self._last_mono_ms is None else max(0, mono_ms - self._last_mono_ms)
        self._last_mono_ms = mono_ms
        checks = list(self._checks)
        total = len(checks)
        order = [checks[(self._next + n) % total] for n in range(total)] if total else []
        due = order[:self.max_checks]
        states: dict[str, str] = {check.name: "skipped" for check in checks}
        findings: list[Finding] = []
        attempted = 0
        for check in due:
            if should_stop is not None and should_stop():
                break
            if deadline - time.monotonic() < self.check_share_seconds:
                findings.append(Finding(MONITOR_NAME, "machine", "pass", "reported", "pass_budget_spent",
                                        f"The health pass ran out of time; {len(due) - attempted} check(s) wait "
                                        "for the next pass."))
                break
            attempted += 1
            share_end = min(deadline, time.monotonic() + self.check_share_seconds)
            states[check.name], found = self._run_check(check, CheckContext(share_end, now_ms, since))
            findings.extend(found)
        left_out = total - len(due)
        if left_out > 0:
            findings.append(Finding(MONITOR_NAME, "machine", "pass", "reported", "pass_check_limit",
                                    f"{left_out} check(s) did not fit in one pass of {self.max_checks}; they run "
                                    "at the next pass."))
        if total:
            self._next = (self._next + attempted) % total
        report = _PassReport(now_ms, int((time.monotonic() - started) * 1000), attempted,
                             tuple(states.items()), tuple(findings))
        with self._publish:
            self._last = report
            self._fixed.extend(f for f in findings if f.outcome == "fixed")

    def _run_check(self, check: Check, context: CheckContext) -> tuple[str, list[Finding]]:
        name = check.name
        earlier = self._threads.get(name)
        if earlier is not None and earlier.is_alive():
            return "still_running", [Finding(name, "machine", "check", "reported", "check_still_running",
                                             f"The {name} check is still running from an earlier pass and was "
                                             "not started again.")]
        box: dict = {}
        done = threading.Event()

        def target() -> None:
            try:
                box["result"] = check.run(context)
            except Exception as error:
                box["error"] = type(error).__name__
            finally:
                done.set()

        worker = threading.Thread(target=target, name=f"health-{name}", daemon=True)
        self._threads[name] = worker
        worker.start()
        if not done.wait(max(0.0, context.deadline - time.monotonic())):
            return "timed_out", [Finding(name, "machine", "check", "reported", "check_timeout",
                                         f"The {name} check took longer than {self.check_share_seconds:g} s and "
                                         "was cut; it is not started again while it still runs.")]
        if "error" in box:
            kind = box["error"]
            subject = kind if _matches(SUBJECT, kind) else "error"
            return "raised", [Finding(name, "machine", subject, "reported", "check_raised",
                                      f"The {name} check failed with {subject}; it runs again at the next pass.")]
        return self._checked(name, box.get("result"))

    @staticmethod
    def _checked(name: str, result: object) -> tuple[str, list[Finding]]:
        if (type(result) not in (list, tuple) or not all(type(f) is Finding and f.check == name for f in result)):
            return "invalid", [Finding(name, "machine", "result", "reported", "check_invalid_result",
                                       f"The {name} check returned something that is not a list of its own "
                                       "findings; it was ignored.")]
        found = list(result)
        if len(found) > MAX_FINDINGS_PER_CHECK:
            cut = len(found) - MAX_FINDINGS_PER_CHECK
            found = found[:MAX_FINDINGS_PER_CHECK] + [
                Finding(name, "machine", "findings", "reported", "findings_truncated",
                        f"The {name} check found {cut} more than the {MAX_FINDINGS_PER_CHECK} shown.")]
        return "ran", found

    def view(self, project_id: str | None) -> dict:
        """The status line's health section for one project (None: machine findings only): the last
        pass, each check's state with the findings this viewer may see, and those findings."""
        with self._publish:
            last, fixed = self._last, list(self._fixed)
        if last is None:
            return {"last_pass_at_ms": None, "pass_ms": None, "summary": "The health monitor has not run a pass yet.",
                    "checks": [], "findings": [], "more": 0}
        reported = [f for f in last.findings if f.outcome == "reported" and f.visible_to(project_id)]
        visible = reported + [f for f in fixed if f.visible_to(project_id)]
        shown = visible[:VIEW_MAX_FINDINGS]
        checks = []
        for name, state in last.states:
            own = [f for f in visible if f.check == name]
            problem = state if state not in ("ran", "skipped") else None
            checks.append({"check": name,
                           "state": problem or ("findings" if any(f in reported for f in own) else
                                                "skipped" if state == "skipped" else "ok"),
                           "findings": len(own)})
        waiting = len(reported)
        if not visible:
            summary = f"Health monitor: {last.attempted} check(s) ran, no findings."
        else:
            summary = (f"Health monitor: {waiting} finding(s) to look at, "
                       f"{len(visible) - waiting} fixed by itself.")
        return {"last_pass_at_ms": last.started_at_ms, "pass_ms": last.duration_ms, "summary": summary,
                "checks": checks, "findings": [f.shown() for f in shown], "more": len(visible) - len(shown)}
