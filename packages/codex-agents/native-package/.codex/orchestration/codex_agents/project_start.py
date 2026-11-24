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
import functools
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
# HIGH 2: a ledger step the ledger refuses is tried this many times at once, this far apart, and then by the starter
# thread every tick until it lands.
SETTLE_TRIES = 3
SETTLE_RETRY_SECONDS = 0.05
HOST_RELATIVE = Path('.codex') / 'orchestration' / project_host_client.HOST_ENTRY


def start_timeout():
    return project_host_client.START_TIMEOUT_SECONDS if START_TIMEOUT_SECONDS is None else START_TIMEOUT_SECONDS


def start_policy():
    """The ledger's backoff/park/recheck parameters, from the constants above (milliseconds)."""
    return {'backoff_first_ms': int(START_BACKOFF_FIRST_SECONDS * 1000),
            'backoff_max_ms': int(START_BACKOFF_MAX_SECONDS * 1000),
            'park_after_ms': int(START_PARK_AFTER_SECONDS * 1000),
            'recheck_ms': int(START_RECHECK_SECONDS * 1000),
            'probation_ms': int(START_PROBATION_SECONDS * 1000)}


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
        self._own = set()  # attempts this starter claimed and has not settled in the ledger
        self._busy = 0  # starts in flight on the workers
        self._reserved = {}  # HIGH 3: attempt -> a dead collector's claim whose child survives; each holds a slot
        self._foreign = {}  # HIGH 4: project id -> a kept probation of a host this starter did not spawn
        self._unsettled = collections.deque()  # HIGH 2: ledger steps refused so far, retried by the starter thread
        self._cursor = None  # HIGH 1: (due time, project id) of the last claimed row; None starts a new pass
        self._children = {}  # pid -> [project_id, attempt, probation end (monotonic) or None once it is over]
        self._children_lock = threading.Lock()
        self._threads = []
        self.most_busy = 0  # the most slots ever in use and the longest queue (read by tests in process)
        self.most_queued = 0

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

    # The starter thread: settle, reap, probation, stranded claims, claims.

    def _main(self):
        from .learning_store import SWEEP_BATCH_GAP_SECONDS, WORKER_RESULT_SWEEP_SECONDS
        tick = min(STARTER_TICK_SECONDS, STARTER_CHECK_SECONDS)
        # E2c-10: stranded claims and kept probations are reconciled first, before the first claim, which follows
        # at once (E2c-1); then once per sweep interval.
        now = time.monotonic()
        next_tick = stranded_at = claim_at = now
        while not self._stop.wait(max(0.0, min(next_tick, stranded_at, claim_at) - time.monotonic())):
            now = time.monotonic()
            try:
                if now >= next_tick:
                    next_tick = now + tick
                    self._retry_unsettled()
                    self._watch_children()
                    self._watch_reserved()
                    self._watch_foreign()
                if now >= stranded_at:
                    stranded_at = now + tick  # a failed pass is tried again at the next tick
                    self._reconcile_stranded()
                    self._reconcile_probations()
                    stranded_at = time.monotonic() + WORKER_RESULT_SWEEP_SECONDS
                if now >= claim_at:
                    claim_at = now + tick  # a refused claim is tried again at the next tick
                    more = self._claim()
                    # HIGH 1: a full batch is followed by the next one at once, so 1,000 due projects are claimed
                    # in successive short ledger holds, not one batch per STARTER_CHECK_SECONDS; a full queue is
                    # looked at again every tick.
                    claim_at = time.monotonic() + (SWEEP_BATCH_GAP_SECONDS if more else
                                                   tick if more is None else STARTER_CHECK_SECONDS)
            except Exception as error:
                self._error(error)
        self._release_queued()
        self._retry_unsettled()

    def _claim(self):
        """One claim batch after the cursor; whether another batch should follow at once (None: the queue is
        full)."""
        with self._condition:
            if len(self._queue) >= START_QUEUE_LIMIT:
                return None  # full: claims resume once the workers have taken some (E2c-2)
            room = START_QUEUE_LIMIT - len(self._queue)
        limit = min(START_CLAIM_BATCH, room)
        after = self._cursor
        claims = self.ledger.claim_due_starts(now_ms=_now_ms(), limit=limit, after=after)
        with self._condition:
            for claim in claims:
                self._own.add(claim.attempt)
                if claim.project_id in self._queued:
                    continue  # cannot happen (one claim per project), never queued twice
                self._queued.add(claim.project_id)
                self._queue.append(claim)
            self.most_queued = max(self.most_queued, len(self._queue))
            self._condition.notify_all()
        if len(claims) == limit:
            self._cursor = (claims[-1].due_at_ms, claims[-1].project_id)
            return True
        # The pass reached the last due row: the next one starts from the first again, at once when this pass
        # started past the first row (rows before the cursor, retries among them, are read only now).
        self._cursor = None
        return after is not None

    def _release_queued(self):
        with self._condition:
            left = list(self._queue)
            self._queue.clear()
            self._queued.clear()
        for claim in left:
            self._settle(claim, tries=1)  # released as claimed; else a later collector reconciles it (no child)

    # HIGH 2: ledger steps never end a worker. A refused step is tried again a few times at once and then by the
    # starter thread every tick until it lands; meanwhile its attempt stays this starter's own.

    def _settle(self, claim, outcome=None, reason=None, child_pid=None, *, tries=SETTLE_TRIES):
        """End `claim` in the ledger: released (outcome None) or finished with its outcome. Never raises."""
        step = functools.partial(self._settle_once, claim, outcome, reason, child_pid)
        return self._step(step, tries=tries)

    def _settle_once(self, claim, outcome, reason, child_pid):
        if outcome is None:
            self.ledger.release_start(claim.project_id, claim.attempt, now_ms=_now_ms())
        else:
            self.ledger.finish_start(claim.project_id, claim.attempt, outcome=outcome, reason=reason,
                                     now_ms=_now_ms(), child_pid=child_pid, **start_policy())
        with self._condition:
            self._own.discard(claim.attempt)

    def _step(self, step, *, tries=SETTLE_TRIES):
        for attempt in range(tries):
            try:
                step()
                return True
            except Exception as error:
                self._error(error)
                if attempt + 1 < tries and self._stop.wait(SETTLE_RETRY_SECONDS):
                    break
        with self._condition:
            self._unsettled.append(step)
        return False

    def _retry_unsettled(self):
        with self._condition:
            steps = list(self._unsettled)
            self._unsettled.clear()
        for step in steps:
            try:
                step()
            except Exception as error:
                self._error(error)
                with self._condition:
                    self._unsettled.append(step)

    def _probation(self, project_id, attempt, survived):
        self._step(functools.partial(self.ledger.start_probation, project_id, attempt=attempt, survived=survived,
                                     now_ms=_now_ms(), **start_policy()), tries=1)

    def _watch_children(self):
        """Reap this starter's hosts; one that ends within its probation counts failed, one that serves it out ends
        its streak (E2c-4)."""
        now = time.monotonic()
        with self._children_lock:
            children = list(self._children.items())
        for pid, (project_id, attempt, probation_end) in children:
            try:
                ended = os.waitpid(pid, os.WNOHANG)[0] == pid
            except ChildProcessError:
                ended = True
            if ended:
                with self._children_lock:
                    self._children.pop(pid, None)
                if probation_end is not None:
                    self._probation(project_id, attempt, False)
            elif probation_end is not None and now >= probation_end:
                with self._children_lock:
                    if pid in self._children:
                        self._children[pid][2] = None
                self._probation(project_id, attempt, True)

    def _own_probations(self):
        with self._children_lock:
            return {attempt for _project, attempt, end in self._children.values() if end is not None}

    def _watch_reserved(self):
        """HIGH 3: a dead collector's surviving child holds its slot until it serves, ends or reaches twice the start
        timeout (released as failed, never signalled: its identity cannot be proved after a restart)."""
        with self._condition:
            reserved = list(self._reserved.values())
        limit_ms = int(2 * start_timeout() * 1000)
        for row in reserved:
            claim = types.SimpleNamespace(project_id=row['project_id'], attempt=row['attempt'])
            pid = row['child_pid']
            if project_host_client.project_host_serving(self.state_root, row['canonical_root']):
                self._settle(claim, 'started', child_pid=pid, tries=1)
                self._foreign.setdefault(row['project_id'], {
                    'project_id': row['project_id'], 'probation_attempt': row['attempt'], 'probation_pid': pid,
                    'probation_until_ms': _now_ms() + int(START_PROBATION_SECONDS * 1000),
                    'canonical_root': row['canonical_root']})
            elif not _group_alive(pid):
                self._settle(claim, 'failed', 'service_ended', tries=1)
            elif _now_ms() - int(row['claimed_at_ms'] or 0) >= limit_ms:
                self._settle(claim, 'failed', 'start_timeout', tries=1)
            else:
                continue
            with self._condition:
                self._reserved.pop(row['attempt'], None)
                self._condition.notify_all()

    def _watch_foreign(self):
        """HIGH 4: a kept probation of a host a previous collector started: it fails when its host ends before the
        probation's end, and ends the streak once its host outlives it."""
        now = _now_ms()
        for project_id, row in list(self._foreign.items()):
            pid = row['probation_pid']
            alive = (_group_alive(pid) if pid is not None
                     else project_host_client.project_host_serving(self.state_root, row['canonical_root']))
            until = int(row['probation_until_ms'] or 0)
            if alive and now < until:
                continue
            self._probation(project_id, row['probation_attempt'], alive or now >= until)
            self._foreign.pop(project_id, None)

    def _reconcile_stranded(self):
        """E2c-10: claims this starter does not own (a dead collector left them): released when no child or a
        child gone; kept while a child survives (holding a slot, HIGH 3), released as failed past twice the
        timeout, never signalled."""
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
            if row['attempt'] in self._own or row['attempt'] in self._reserved:
                return
        pid = row['child_pid']
        if pid is None:
            self.ledger.release_start(row['project_id'], row['attempt'], now_ms=_now_ms())
        elif project_host_client.project_host_serving(self.state_root, row['canonical_root']):
            self.ledger.finish_start(row['project_id'], row['attempt'], outcome='started', reason=None,
                                     now_ms=_now_ms(), child_pid=pid, **start_policy())  # its probation is kept
        elif not _group_alive(pid):
            self.ledger.finish_start(row['project_id'], row['attempt'], outcome='failed', reason='service_ended',
                                     now_ms=_now_ms(), **start_policy())
        elif _now_ms() - int(row['claimed_at_ms'] or 0) >= limit_ms:
            self.ledger.finish_start(row['project_id'], row['attempt'], outcome='failed', reason='start_timeout',
                                     now_ms=_now_ms(), **start_policy())
        else:
            with self._condition:  # its slot is held from now on, watched every tick (_watch_reserved)
                self._reserved[row['attempt']] = row
                self._own.add(row['attempt'])

    def _reconcile_probations(self):
        """HIGH 4: every kept probation this starter does not watch itself (a previous collector's) is watched."""
        own = self._own_probations()
        after = ''
        while not self._stop.is_set():
            page = self.ledger.probation_starts(after=after, limit=START_CLAIM_BATCH)
            for row in page:
                if row['probation_attempt'] not in own:
                    self._foreign.setdefault(row['project_id'], row)
            if len(page) < START_CLAIM_BATCH:
                return
            after = page[-1]['project_id']

    # The workers: one start at a time each; a slot a dead collector's surviving child holds is not used (HIGH 3).

    def _worker(self):
        while not self._stop.is_set():
            claim = None
            try:
                with self._condition:
                    while not self._stop.is_set() and (
                            not self._queue or self._busy + len(self._reserved) >= START_SLOTS):
                        self._condition.wait(0.5)
                    if self._stop.is_set():
                        return
                    claim = self._queue.popleft()
                    self._queued.discard(claim.project_id)
                    self._busy += 1
                    self.most_busy = max(self.most_busy, self._busy + len(self._reserved))
                try:
                    self._attempt(claim)
                except Exception as error:
                    self._error(error)
                    self._settle(claim)
            except Exception as error:  # a worker never ends before its collector stops (HIGH 2)
                self._error(error)
            finally:
                if claim is not None:
                    with self._condition:
                        self._busy -= 1
                        self._condition.notify_all()

    def _attempt(self, claim):
        blocked = self._pre_start(claim)
        if blocked is not None:
            self._settle(claim, *blocked)
            return
        from .host_commands import HostCommandError, launch_target_problem, pin_runtime_copy
        copy, root = Path(claim.copy), Path(claim.root)
        try:
            with pin_runtime_copy(copy):  # E2c-5: pinned from its verification until the child serves or ends
                if copy.name != claim.digest or launch_target_problem(copy, root, {}) is not None:
                    self._settle(claim, 'install_changed')
                    return
                outcome = self._spawn(claim, copy / HOST_RELATIVE)
        except HostCommandError:
            self._settle(claim, 'install_changed')  # gone, or being changed by setup right now
            return
        if outcome[0] == 'left':
            if outcome[1]:
                with self._condition:
                    self._own.discard(claim.attempt)  # its child is on record: the next collector reconciles it
            else:
                self._settle(claim)
            return
        outcome, reason, pid = outcome
        self._settle(claim, outcome, reason, child_pid=pid)
        if outcome == 'started':
            with self._children_lock:  # after the ledger keeps its probation (HIGH 4)
                self._children[pid] = [claim.project_id, claim.attempt, time.monotonic() + START_PROBATION_SECONDS]

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
        timeout (then its process group is killed). Returns (outcome, reason, pid), or ('left', recorded) when the
        collector stops first (the child is left to run; with its pid on record the next collector reconciles it).
        HIGH 2: from the spawn on, this worker owns the child whatever fails: a pid record the ledger refuses is
        tried again and otherwise only missing from the ledger, and an unexpected error kills and reaps the child."""
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
            reaped = False
            try:
                kill_at = time.monotonic() + 2 * start_timeout()
                recorded = self._record_child(claim, pid)
                while True:
                    if project_host_client.project_host_serving(self.state_root, root):
                        return ('started', None, pid)
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        reaped = True
                        if project_host_client.project_host_serving(self.state_root, root):
                            return ('serving', None, None)  # another start's host serves it
                        reason = project_host_client._ended_report(report_path).get('reason_code')
                        return ('failed', reason or 'service_ended', None)
                    if time.monotonic() >= kill_at:
                        _kill_and_reap(pid)
                        reaped = True
                        return ('failed', 'start_timeout', None)
                    if self._stop.is_set():
                        with self._children_lock:
                            self._children[pid] = [claim.project_id, claim.attempt, None]
                        reaped = True  # reaped by the starter thread or left to the system at exit
                        return ('left', recorded)
                    time.sleep(0.05)
            except BaseException:
                if not reaped:
                    _kill_and_reap(pid)
                raise
        finally:
            try:
                report_path.unlink()
            except OSError:
                pass

    def _record_child(self, claim, pid):
        """E2c-10: the child's pid on its claim, for a later collector; whether the ledger kept it (HIGH 2)."""
        for attempt in range(SETTLE_TRIES):
            try:
                self.ledger.set_start_child(claim.project_id, claim.attempt, pid)
                return True
            except Exception as error:
                self._error(error)
                if attempt + 1 < SETTLE_TRIES:
                    time.sleep(SETTLE_RETRY_SECONDS)
        return False


def _kill_and_reap(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _group_alive(pid):
    try:
        os.killpg(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, owned by someone else
    return True
