"""HM-1: every COCOLO run container belongs to a live run (PLAN-0.4.0-supervisor-slices.md, "HM-1 Containers").

The health monitor's first check (HM-0's `Check`), run in the collector's health pass, machine wide. C4's
recovery already sweeps a project's orphans from that project's own service, but only while that service
runs; this check covers every project of the machine whatever its service is doing, and says so in status.

Where the live set comes from: every project service of this machine keeps its launch journal under the
collector's own state root (`<state root>/projects/<project>/launches`, `project_driver.RunDriver`), so the
collector reads it there, read only (`launch_journal.container_tokens`: file names only; no lock is taken and
nothing is written). The live set is C4's: every container entry, whatever its state and whichever instance
wrote it (this one, another live one, a stopped one whose entry C4's recovery owns), and an entry file that
cannot be read counts too. So a lock's state never matters here: an entry is live while its file exists.

One pass, on the check's own worker thread (a stop can take the grace and the gone timeout, far longer than
the check's share of the pass; `run` waits for it only while its share lasts, and a worker still running at
the next pass is left to finish, never doubled):

1. Containers off (`container_launch.SETTINGS` None, as shipped) and no container entry in any project's
   journal: nothing at all, no podman call (podman, its machine or service are never started anyway: a
   stopped machine is only "unreachable").
2. Every run container on this podman is listed once (`container_runtime.list_runs`, label
   `cocolo.kind=run`); podman unreachable or missing: reported, nothing changed.
3. A container whose `cocolo.project` is not a project of this state root (another COCOLO install's, a
   test's, a project id of another shape) is never touched; they are reported together, once.
4. For each project of this state root in the listing, its live set is read after the listing: every intent
   is written before its container is created, so a run launched meanwhile is in it. A live set that cannot
   be read completely (the journal folder missing, not owner-only, unlistable): nothing of that project is
   stopped, and that is reported to the project.
5. Each listed container of the project whose run is not live is stopped through the one stop action
   (`ContainerRuntime.stop_orphans`: only a container named as its run's container and carrying the run's
   labels; SIGTERM, the grace, SIGKILL, removal, then "gone" asked of the OS). Removed: `fixed`. Not proven
   gone: `orphan_stop_failed`, reported; it is still listed at the next pass and tried again then. A
   container with the project's labels but another name is reported and left alone. At most
   MAX_STOPS_PER_PASS stops per pass; the others wait for the next pass and are reported waiting.

Findings of a project go to that project only; podman's state and the foreign containers to the machine.
Standard library only.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import threading
import time

from . import container_launch
from .container_runtime import (DEFAULT_GRACE_SECONDS, DEFAULT_NAME_PREFIX, PROJECT_LABEL, RUN_LABEL,
                                ContainerRuntime, Refusal, SubprocessPodman, list_runs)
from .health_monitor import MAX_TEXT, PROJECT_ID, SUBJECT, Check, Finding
from .launch_journal import container_tokens

CHECK_NAME = 'containers'
# Orphans stopped in one pass (each stop may take the grace and the gone timeout); the rest wait for the next.
MAX_STOPS_PER_PASS = 4
# `run` gives back its answer this long before its share ends, so the monitor never cuts it.
RESULT_MARGIN_SECONDS = 0.1


def _sentence(text):
    """One printable sentence of at most MAX_TEXT characters (podman's words are not trusted to be either)."""
    clean = ''.join(character if character.isprintable() else ' ' for character in str(text))
    return clean[:MAX_TEXT]


def _subject(*candidates):
    for value in candidates:
        if type(value) is str and SUBJECT.fullmatch(value):
            return value
    return 'container'


class _Job:
    def __init__(self):
        self.done = threading.Event()
        self.findings = []


class ContainerCheck(Check):
    """HM-1's check over one state root (see the module notes). `settings` gives the container switch at each
    pass (default: `container_launch.SETTINGS`); `runner` is podman (default: the client at its fixed path)."""

    name = CHECK_NAME

    def __init__(self, state_root, *, settings=None, runner=None, host_files=None, monotonic=time.monotonic,
                 sleep=time.sleep):
        self.projects = Path(state_root) / 'projects'
        self._settings = settings if settings is not None else (lambda: container_launch.SETTINGS)
        self._runner, self._host_files = runner, host_files
        self._monotonic, self._sleep = monotonic, sleep
        self._job = None  # a worker still running from an earlier pass
        self._reported = []  # the last finished pass's reported findings, shown until the next one finishes

    def run(self, context):
        earlier = self._job
        harvested = []
        if earlier is not None:
            if not earlier.done.is_set():
                return list(self._reported)  # still stopping: never a second worker
            self._job, harvested = None, list(earlier.findings)
            self._reported = [item for item in harvested if item.outcome == 'reported']
        settings = self._settings()
        if settings is None and not self._containers_used():
            self._reported = []
            return harvested
        job = _Job()
        worker = threading.Thread(target=self._work, args=(job, settings), name='health-containers', daemon=True)
        worker.start()
        if job.done.wait(max(0.0, context.deadline - time.monotonic() - RESULT_MARGIN_SECONDS)):
            self._reported = [item for item in job.findings if item.outcome == 'reported']
            # The earlier worker's fixes are shown once; its reported findings are replaced by this pass's.
            return [item for item in harvested if item.outcome == 'fixed'] + list(job.findings)
        self._job = job
        return harvested or list(self._reported)

    def _containers_used(self):
        """Whether any project's journal holds a container entry (read only; no podman call)."""
        try:
            names = os.listdir(self.projects)
        except OSError:
            return False
        return any(PROJECT_ID.fullmatch(name) and container_tokens(self.projects / name / 'launches')
                   for name in names)

    def _work(self, job, settings):
        try:
            job.findings = self._sweep(settings)
        except Exception as error:
            kind = type(error).__name__
            job.findings = [Finding(CHECK_NAME, 'machine', _subject(kind, 'error'), 'reported', 'container_check_failed',
                                    _sentence(f'The container check failed with {kind}; no container was '
                                              'stopped by it, and it runs again at the next pass.'))]
        finally:
            job.done.set()

    def _known(self, project_id):
        """A project of this state root: its own folder under projects/ (never a link)."""
        try:
            info = os.lstat(self.projects / project_id)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()

    def _sweep(self, settings):
        runner = self._runner if self._runner is not None else SubprocessPodman()
        rows = list_runs(runner)
        if isinstance(rows, Refusal):
            return [Finding(CHECK_NAME, 'machine', 'podman', 'reported', 'podman_unavailable',
                            _sentence('The container check could not list containers, so it stopped none: '
                                      + rows.message))]
        by_project, foreign = {}, 0
        for row in rows:
            project_id = row['Labels'].get(PROJECT_LABEL)
            if type(project_id) is not str or not PROJECT_ID.fullmatch(project_id) or not self._known(project_id):
                foreign += 1  # another install's, a test's, or no project at all: never touched
                continue
            by_project.setdefault(project_id, []).append(row)
        prefix = settings.name_prefix if settings is not None else DEFAULT_NAME_PREFIX
        grace = settings.grace_seconds if settings is not None else DEFAULT_GRACE_SECONDS
        findings, waiting, stops = [], 0, 0
        for project_id in sorted(by_project):
            live = container_tokens(self.projects / project_id / 'launches')  # after the listing
            if live is None:
                findings.append(Finding(CHECK_NAME, 'project', 'launches', 'reported', 'live_set_unreadable',
                                        "This project's launch journal could not be read, so none of its "
                                        'containers was stopped; it is checked again at the next pass.',
                                        project_id))
                continue
            candidates = [row for row in by_project[project_id] if row['Labels'].get(RUN_LABEL) not in live]
            due = []
            for row in candidates:
                if stops >= MAX_STOPS_PER_PASS:
                    waiting += 1
                    continue
                stops += 1
                due.append(row)
            if not due:
                continue
            runtime = ContainerRuntime(project_id, name_prefix=prefix, runner=runner, host_files=self._host_files,
                                       monotonic=self._monotonic, sleep=self._sleep)
            report = runtime.stop_orphans(due, live, grace_seconds=grace)
            findings.extend(self._finding(project_id, entry) for entry in report.entries)
        if waiting:
            findings.append(Finding(CHECK_NAME, 'machine', 'orphans', 'reported', 'orphans_waiting',
                                    f'{waiting} more container(s) with no live run wait for the next pass; at most '
                                    f'{MAX_STOPS_PER_PASS} are stopped in one pass.'))
        if foreign:
            findings.append(Finding(CHECK_NAME, 'machine', 'foreign', 'reported', 'foreign_containers',
                                    f'{foreign} COCOLO run container(s) belong to no project of this COCOLO; they '
                                    'were left alone.'))
        order = {'orphan_stop_failed': 0, 'orphan_stop_refused': 0, 'live_set_unreadable': 1, 'orphan_removed': 2}
        return sorted(findings, key=lambda item: order.get(item.reason_code, 3))

    @staticmethod
    def _finding(project_id, entry):
        subject = _subject(entry.name, entry.container_id[:12])
        if entry.outcome == 'removed':
            outcome, reason = 'fixed', 'orphan_removed'
            text = f'The container {subject} belonged to no live run of this project; it was stopped and removed.'
        elif entry.outcome == 'skipped':
            outcome, reason = 'reported', 'container_not_a_run'
            text = (f"The container {subject} carries this project's labels but is not named as its run's "
                    'container; it was left alone.')
        elif entry.outcome == 'refused':
            outcome, reason = 'reported', 'orphan_stop_refused'
            text = (f'The container {subject} belongs to no live run of this project, but podman stopped answering '
                    'while it was stopped; it is tried again at the next pass.')
        else:
            outcome, reason = 'reported', 'orphan_stop_failed'
            text = (f'The container {subject} belongs to no live run of this project and could not be proven gone '
                    'after its stop; it is tried again at the next pass.')
        return Finding(CHECK_NAME, 'project', subject, outcome, reason, _sentence(text), project_id)
