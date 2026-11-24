"""RS-E E2c: the collector's starter for stopped projects with due holds (docs/requirements/e2c/requirements.md).

The collector's own starter thread claims due projects in one short ledger step (`claim_due_starts`) and hands each
claim to one of START_SLOTS workers. A worker does the unattended pre-start steps, verifies the project's
setup-bound launch record (its protected runtime copy, pinned while it is verified and launched) and spawns that
copy's project host with a bare environment, all off the ledger lock and off the collector's request thread. Every
outcome goes back to the ledger in one short step (`finish_start`): failures back off and, once a failure streak is
an hour old, park until the user acts (owner decision 143). Nothing here migrates learning, takes collector
maintenance or stops the collector.
"""
from __future__ import annotations

import collections
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import time
import types

from .collector_paths import CollectorPaths
from .host_paths import PYTHON_ISOLATION_FLAGS
from . import project_host_client

# E2c named constants (requirements.md). Due rows are looked for every STARTER_CHECK_SECONDS (well inside the
# collector's 60 s sweep interval, E2c-1).
STARTER_CHECK_SECONDS = 5.0
START_SLOTS = 2
START_CLAIM_BATCH = 64
START_QUEUE_LIMIT = 1024
START_BACKOFF_FIRST_SECONDS = 30.0
START_BACKOFF_MAX_SECONDS = 900.0
START_PROBATION_SECONDS = 60.0
START_PARK_AFTER_SECONDS = 3600.0
START_RECHECK_SECONDS = 60.0
# None: project_host_client.START_TIMEOUT_SECONDS at the time of the start. A child still alive at twice this is killed.
START_TIMEOUT_SECONDS = None
# How often the starter reaps its children, watches their probation and fills free slots.
STARTER_TICK_SECONDS = 1.0
HOST_RELATIVE = Path('.codex') / 'orchestration' / project_host_client.HOST_ENTRY


def start_timeout():
    return project_host_client.START_TIMEOUT_SECONDS if START_TIMEOUT_SECONDS is None else START_TIMEOUT_SECONDS


def start_policy():
    """The ledger's backoff/park/recheck parameters, from the constants above (milliseconds)."""
    return {'backoff_first_ms': int(START_BACKOFF_FIRST_SECONDS * 1000),
            'backoff_max_ms': int(START_BACKOFF_MAX_SECONDS * 1000),
            'park_after_ms': int(START_PARK_AFTER_SECONDS * 1000),
            'recheck_ms': int(START_RECHECK_SECONDS * 1000)}


def _now_ms():
    return time.time_ns() // 1_000_000


def _utc(ms):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ms / 1000))


def recovery_line(record, root, now_ms):
    """E2c-9: the status line for one project's start record (`LearningLedger.start_record`), or None. Plain text
    from fixed codes and times only: never hold or result content."""
    command = f'run learning-start-project-service --project-root {root}'
    if record.get('parked_reason'):
        return (f"recovery failed: {record['parked_reason']}; stopped trying after an hour of failed starts: "
                f"{command}")
    if record.get('attempt'):
        return 'recovery: starting'
    outcome = record.get('outcome')
    if outcome == 'failed' and record.get('next_start_at_ms') is not None:
        return (f"recovery: backing off ({record['failures']} failed: {record['reason']}, "
                f"next {_utc(record['next_start_at_ms'])})")
    if outcome == 'install_changed':
        return 'recovery pending: install changed'
    if outcome == 'handover':
        return f'recovery: needs update handover: {command}'
    if outcome == 'migration':
        return f'recovery pending: learning migration: {command}'
    if outcome == 'serving':
        due = record.get('due_at_ms')
        if due is not None and due <= now_ms - int(START_RECHECK_SECONDS * 1000):
            return 'recovery pending: service unresponsive'
        return None
    if outcome == 'started' and record.get('started_at_ms') is not None:
        return f"recovery: started {_utc(record['started_at_ms'])}"
    return None


class ProjectStarter:
    """E2c-1..10: the starter thread and its START_SLOTS workers for one running CollectorServer."""

    def __init__(self, server):
        self.server = server
        self.ledger = server.ledger
        self.state_root = Path(server.state.root)
        self._stop = server._sweeps_stop
        self._condition = threading.Condition()
        self._queue = collections.deque()
        self._queued = set()
        self._own = set()  # attempts this starter claimed and has not ended
        self._children = {}  # pid -> [project_id, probation end (monotonic) or None once it is over]
        self._children_lock = threading.Lock()
        self._threads = []

    def start(self):
        self._threads = [threading.Thread(target=self._main, name='collector-starter', daemon=True)]
        self._threads += [threading.Thread(target=self._worker, name=f'collector-start-{index}', daemon=True)
                          for index in range(START_SLOTS)]
        for thread in self._threads:
            thread.start()

    def join(self, timeout):
        end = time.monotonic() + timeout
        with self._condition:
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(max(0.0, end - time.monotonic()))

    def _error(self, error):
        self.server.sweep_error = type(error).__name__

    # The starter thread: reap, probation, stranded claims, claims.

    def _main(self):
        claim_at = time.monotonic() + STARTER_CHECK_SECONDS
        stranded_at = claim_at  # E2c-10: before the first claim, then once per sweep interval
        while not self._stop.wait(min(STARTER_TICK_SECONDS, STARTER_CHECK_SECONDS)):
            try:
                self._watch_children()
                now = time.monotonic()
                if now >= stranded_at:
                    from .learning_store import WORKER_RESULT_SWEEP_SECONDS
                    self._reconcile_stranded()
                    stranded_at = now + WORKER_RESULT_SWEEP_SECONDS
                if now >= claim_at:
                    self._claim()
                    claim_at = time.monotonic() + STARTER_CHECK_SECONDS
            except Exception as error:
                self._error(error)
        self._release_queued()

    def _claim(self):
        with self._condition:
            room = START_QUEUE_LIMIT - len(self._queue)
        if room <= 0:
            return
        claims = self.ledger.claim_due_starts(now_ms=_now_ms(), limit=min(START_CLAIM_BATCH, room))
        with self._condition:
            for claim in claims:
                self._own.add(claim.attempt)
                if claim.project_id in self._queued:
                    continue  # cannot happen (one claim per project), never queued twice
                self._queued.add(claim.project_id)
                self._queue.append(claim)
            self._condition.notify_all()

    def _release_queued(self):
        with self._condition:
            left = list(self._queue)
            self._queue.clear()
            self._queued.clear()
        for claim in left:
            try:
                self.ledger.release_start(claim.project_id, claim.attempt, now_ms=_now_ms())
            except Exception:
                pass  # a later collector reconciles it (no child)

    def _finish(self, claim, outcome, reason=None):
        try:
            self.ledger.finish_start(claim.project_id, claim.attempt, outcome=outcome, reason=reason,
                                     now_ms=_now_ms(), **start_policy())
        finally:
            with self._condition:
                self._own.discard(claim.attempt)

    def _watch_children(self):
        """Reap this starter's hosts; one that ends within its probation counts failed, one that serves it out ends
        its streak (E2c-4)."""
        now = time.monotonic()
        with self._children_lock:
            children = list(self._children.items())
        for pid, (project_id, probation_end) in children:
            try:
                ended = os.waitpid(pid, os.WNOHANG)[0] == pid
            except ChildProcessError:
                ended = True
            if ended:
                with self._children_lock:
                    self._children.pop(pid, None)
                if probation_end is not None:
                    self.ledger.start_probation(project_id, survived=False, now_ms=_now_ms(), **start_policy())
            elif probation_end is not None and now >= probation_end:
                self.ledger.start_probation(project_id, survived=True, now_ms=_now_ms(), **start_policy())
                with self._children_lock:
                    if pid in self._children:
                        self._children[pid][1] = None

    def _reconcile_stranded(self):
        """E2c-10: claims this starter does not own (a dead collector left them): released when no child or a
        child gone; kept while a child survives, released as failed past twice the timeout, never signalled."""
        limit_ms = int(2 * start_timeout() * 1000)
        after = ''
        while not self._stop.is_set():
            page = self.ledger.outstanding_starts(after=after, limit=START_CLAIM_BATCH)
            for row in page:
                self._reconcile_claim(row, limit_ms)
            if len(page) < START_CLAIM_BATCH:
                return
            after = page[-1]['project_id']

    def _reconcile_claim(self, row, limit_ms):
        with self._condition:
            if row['attempt'] in self._own:
                return
        claim = types.SimpleNamespace(project_id=row['project_id'], attempt=row['attempt'])
        serving = project_host_client.project_host_serving(self.state_root, row['canonical_root'])
        pid = row['child_pid']
        if pid is None:
            self.ledger.release_start(row['project_id'], row['attempt'], now_ms=_now_ms())
        elif serving:
            self._finish(claim, 'started')
        elif _group_alive(pid):
            if _now_ms() - int(row['claimed_at_ms'] or 0) >= limit_ms:
                self._finish(claim, 'failed', 'start_timeout')
        else:
            self._finish(claim, 'failed', 'service_ended')

    # The workers: one start at a time each.

    def _worker(self):
        while True:
            with self._condition:
                while not self._queue and not self._stop.is_set():
                    self._condition.wait(0.5)
                if self._stop.is_set():
                    return
                claim = self._queue.popleft()
                self._queued.discard(claim.project_id)
            try:
                self._attempt(claim)
            except Exception as error:
                self._error(error)
                try:
                    self.ledger.release_start(claim.project_id, claim.attempt, now_ms=_now_ms())
                finally:
                    with self._condition:
                        self._own.discard(claim.attempt)

    def _attempt(self, claim):
        blocked = self._pre_start(claim)
        if blocked is not None:
            self._finish(claim, *blocked)
            return
        from .host_commands import HostCommandError, launch_target_problem, pin_runtime_copy
        copy, root = Path(claim.copy), Path(claim.root)
        try:
            with pin_runtime_copy(copy):  # E2c-5: pinned from its verification until the child serves or ends
                if copy.name != claim.digest or launch_target_problem(copy, root, {}) is not None:
                    self._finish(claim, 'install_changed')
                    return
                outcome = self._spawn(claim, copy / HOST_RELATIVE)
        except HostCommandError:
            self._finish(claim, 'install_changed')  # gone, or being changed by setup right now
            return
        if outcome is None:
            self.ledger.release_start(claim.project_id, claim.attempt, now_ms=_now_ms())
            with self._condition:
                self._own.discard(claim.attempt)
            return
        self._finish(claim, *outcome)

    def _pre_start(self, claim):
        """E2c-6/7/8: why this project is not started now, as (outcome, reason), or None to start it."""
        if claim.copy is None or claim.digest is None:
            return ('no_record', None)
        from .collector_runtime import require_authorized_collector_runtime
        try:
            require_authorized_collector_runtime(CollectorPaths(self.state_root))
        except Exception:
            return ('unauthorized', 'runtime_unauthorized')
        root = Path(claim.root)
        if project_host_client.other_version_host(self.state_root, root) is not None:
            return ('handover', None)
        if project_host_client.project_host_serving(self.state_root, root):
            return ('serving', None)
        if self.ledger.learning_activation_pending(claim.project_id):
            return ('migration', None)
        if not self.ledger.learning_moved(claim.project_id) and not self.ledger.mark_learning_moved_if_empty(
                claim.project_id):
            return ('migration', None)
        return None

    def _spawn(self, claim, entry):
        """E2c-5/10: spawn the copy's host and wait, off the lock, until it serves, ends or reaches twice the start
        timeout (then its process group is killed). Returns (outcome, reason), or None when the collector stops
        first (the child is left to run; it is not this start's to finish)."""
        root = Path(claim.root)
        report_path = self.state_root / f'.project-start-{secrets.token_hex(8)}.json'
        actions = [(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDWR, 0),
                   (os.POSIX_SPAWN_OPEN, 1, str(report_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_RDWR, 0)]
        try:
            pid = os.posix_spawn(
                sys.executable,
                [sys.executable, *PYTHON_ISOLATION_FLAGS, str(entry), '--state-root', str(self.state_root),
                 '--project-root', str(root)],
                {'PATH': os.defpath, 'xcrun_nocache': '1'}, file_actions=actions, setsid=True)
            self.ledger.set_start_child(claim.project_id, claim.attempt, pid)
            kill_at = time.monotonic() + 2 * start_timeout()
            while True:
                if project_host_client.project_host_serving(self.state_root, root):
                    with self._children_lock:
                        self._children[pid] = [claim.project_id, time.monotonic() + START_PROBATION_SECONDS]
                    return ('started', None)
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    if project_host_client.project_host_serving(self.state_root, root):
                        return ('serving', None)  # another start's host serves it
                    reason = project_host_client._ended_report(report_path).get('reason_code')
                    return ('failed', reason or 'service_ended')
                if time.monotonic() >= kill_at:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)
                    return ('failed', 'start_timeout')
                if self._stop.is_set():
                    with self._children_lock:
                        self._children[pid] = [claim.project_id, None]
                    return None
                time.sleep(0.05)
        finally:
            try:
                report_path.unlink()
            except OSError:
                pass


def _group_alive(pid):
    try:
        os.killpg(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, owned by someone else
    return True
