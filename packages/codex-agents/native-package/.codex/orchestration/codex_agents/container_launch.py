"""C2: a run's container is recorded before its launch, and its deadline is set on the container.

PLAN-0.4.0-supervisor-slices.md, slice C2 (closes RS-C-1 and RS-C-2 reduced, RS-12, D7, gap C7);
owner decision PLAN-0.4.0.md "Containment decided". It wires the container primitive
(`container_runtime`, CS-1..6) into the project service's launch path:

- Before the claim is handed to the account, the driver writes the run's `intent`
  (`LaunchJournal.intent`): a fresh launch token, the container's name derived from it and its
  labels (`cocolo.project`, `cocolo.run` = the token, `cocolo.kind=run`), and what a fence needs.
  No intent, no claim. The admitted claim is then recorded on that intent.
- At spawn (`OwnedExecutionRegistry.spawn`) the container is launched through
  `ContainerRuntime.launch`, whose `record` reads the admitted intent back before the container is
  created; its id is written as `started` once podman returns. The run's absolute deadline is set
  on the container itself (podman's `--timeout`, enforced by conmon), so it fires even when the
  service has been killed. The bridge reads the container's output through a local attach command
  (`ContainerRuntime.attach_arguments`).
- At cleanup the container is stopped (`stopping`, then `gone` or `stop_failed`); a run whose
  container is not proven gone is never confirmed clean, so it keeps its slot.

C3 (one stop action, PLAN-0.4.0-supervisor-slices.md): every reason a run is stopped for (the
liveness clock's startup and work silence, absolute deadline, cancel and monitor unavailable; the
run's own end; the service closing; a refused launch) goes through `ContainerLauncher.stop`, which
asks `ContainerRuntime.stop` (SIGTERM, the grace, SIGKILL to every process in the container,
removal, then "gone" asked of the OS) and records the first reason. Stops of one container are
serialized: a request while one is under way joins it, and a container proven gone is never
stopped again. A stop whose answer is not "gone" is `stop_failed`: the run is not confirmed clean
(its slot stays held, status names it and alerts the owner) and the stop is tried again by
`retry_stops`, STOP_RETRY_SECONDS doubling to STOP_RETRY_MAX_SECONDS, while the service runs; the
proof of "gone" is handed to whoever asked (`on_gone`). The detached watchdog is given the
container (`watchdog_config`) and stops it through the same primitive once its service has died.

THE SWITCH: `SETTINGS` below. While it is None (as shipped) nothing here is used and every child is
launched as a process, exactly as before. It is flipped by setting it to a `ContainerLaunchConfig`
(the image and the seccomp profile file) once the remaining container slices exist: the COCOLO
machine (C-VM) and the owner's sign-in test, the image with the clients, COCOLO's own credentials
and the per-run network. Until then the command a child is given is the host's, which a container
cannot run. An earlier instance's container entry is never settled by the process rule: C4's
recovery (`container_recovery`) finds its container by name and label, stops it through the same
stop action (reason `service_lost`) and settles the run (a live service settles its own, see
`project_driver`).

Standard library only.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import math
import os
import threading
import time

from .container_runtime import (DEFAULT_GRACE_SECONDS, DEFAULT_NAME_PREFIX, MAX_TIMEOUT_SECONDS, PODMAN_ENVIRONMENT,
                                PROJECT_LABEL, RUN_LABEL, ContainerLimits, ContainerRuntime, ContainerSpec, Refusal,
                                SubprocessPodman)
from .launch_journal import CONTAINER_STOP_REASONS, JournalError, new_token
from .process_tree import RUN_TAG_VARIABLE


@dataclass(frozen=True)
class ContainerLaunchConfig:
    image: str
    seccomp_profile: str
    limits: ContainerLimits = field(default_factory=ContainerLimits)
    workspace_writable: bool = False
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    name_prefix: str = DEFAULT_NAME_PREFIX


# C3: a stop whose answer was not "gone" is tried again after this long, doubling up to the cap.
STOP_RETRY_SECONDS = 1.0
STOP_RETRY_MAX_SECONDS = 300.0
# Stops of different containers run at once on at most this many helper threads.
STOP_THREADS = 4


def stop_retry_delay(failures):
    """The wait before the next stop of a container whose last `failures` stops were not "gone"."""
    return min(STOP_RETRY_MAX_SECONDS, STOP_RETRY_SECONDS * 2 ** max(0, min(failures, 64) - 1))


# The switch. None: today's process launch (the default). A ContainerLaunchConfig: every run of a
# project service is launched in a container, recorded before its claim (see the module notes).
SETTINGS: ContainerLaunchConfig | None = None


def launcher_for(project_id, journal, settings=None):
    """The project service's launcher, or None while the switch is off."""
    config = SETTINGS if settings is None else settings
    if config is None:
        return None
    return ContainerLauncher(ContainerRuntime(project_id, name_prefix=config.name_prefix), journal, config)


class ContainerLaunchRefused(RuntimeError):
    """The container was not started; `reason` is the primitive's refusal reason (or the journal's)."""

    def __init__(self, reason, message, refusal=None):
        super().__init__(message)
        self.reason, self.refusal = reason, refusal


@dataclass(frozen=True)
class StartedContainer:
    token: str
    name: str
    container_id: str
    cgroup_path: str


@dataclass
class _Stop:
    """One container's stops: serialized by `lock`; the first reason; proven gone or when to try again."""
    started: StartedContainer
    reason: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    attempts: int = 0
    gone: bool = False
    retry_at: float | None = None
    listeners: list = field(default_factory=list)


class ContainerLauncher:
    def __init__(self, runtime, journal, config, *, attach=None, clock=time.monotonic):
        if not isinstance(runtime, ContainerRuntime) or not isinstance(config, ContainerLaunchConfig):
            raise ValueError('a container launcher needs a ContainerRuntime and a ContainerLaunchConfig')
        self.runtime, self.journal, self.config = runtime, journal, config
        # Tests only: the local command that stands in for the attached container output.
        self._attach = attach
        self._clock = clock
        self._stops, self._stops_lock = {}, threading.Lock()  # token -> _Stop
        self._executor, self._closed = None, False

    def intent(self, *, run_handle, attempt, host_client, lease, identity_digest, claim_request_id,
               storage_evidence, workspace):
        """Write the run's intent before its claim is sent; the launch token, or JournalError."""
        token = new_token()
        identity = self.runtime.identity(token)
        self.journal.intent(token, project_id=self.runtime.project_id, container_name=identity.name,
                            labels=identity.labels, run_handle=run_handle, attempt=attempt,
                            host_client=host_client, lease=lease, identity_digest=identity_digest,
                            claim_request_id=claim_request_id, storage_evidence=storage_evidence,
                            workspace=workspace)
        return token

    def start(self, bridge_run_id, arguments, *, workspace, run_tag, deadline_ns):
        """Launch the admitted run's container; `started` is on disk when this returns.

        The child's command runs inside, in the workspace (mounted at /workspace), with only the run's
        marker in its environment. The deadline (monotonic ns) is set on the container in whole
        seconds, rounded up. Raises ContainerLaunchRefused; a container it created is removed first
        and the journal says whether it is gone.
        """
        entry = self.journal.container_entry(bridge_run_id)
        if entry is None or entry['state'] != 'intent':
            raise ContainerLaunchRefused('no_intent', 'the run has no admitted container intent; nothing was started')
        token = entry['token']
        remaining = (deadline_ns - time.monotonic_ns()) / 1e9
        if remaining <= 0:
            raise ContainerLaunchRefused('deadline_passed', 'the run\'s deadline passed before its container started')
        spec = ContainerSpec(run_id=token, image=self.config.image, command=tuple(arguments),
                             workspace=os.path.realpath(str(workspace)), seccomp_profile=self.config.seccomp_profile,
                             workspace_writable=self.config.workspace_writable,
                             environment={RUN_TAG_VARIABLE: run_tag}, limits=self.config.limits,
                             timeout_seconds=min(MAX_TIMEOUT_SECONDS, max(1, math.ceil(remaining))))

        def record(identity):
            if identity.name != entry['container_name'] or identity.labels != entry['labels']:
                raise JournalError('the container is not the one the intent names')
            self.journal.check_intent(token, identity.name)

        result = self.runtime.launch(spec, record)
        if isinstance(result, Refusal):
            if result.cleanup is not None:
                self._record_stop(token, 'launch_refused', result.cleanup.gone, result.cleanup.detail)
            raise ContainerLaunchRefused(result.reason, result.message, result)
        started = StartedContainer(token, result.identity.name, result.container_id, result.cgroup_path)
        try:
            self.journal.started(token, result.container_id, result.cgroup_path)
        except JournalError as error:
            self.stop(started, 'record_failed')
            raise ContainerLaunchRefused('record_failed', 'the started container could not be recorded, so it was '
                                         'stopped') from error
        return started

    def attach_arguments(self, started):
        if self._attach is not None:
            return list(self._attach(started))
        return self.runtime.attach_arguments(started.name)

    def watchdog_config(self, started):
        """C3: what the detached watchdog needs to stop this container through `ContainerRuntime.stop`
        once the service that launched it has died: {'argument': JSON, 'environment': the podman client's
        variables}. None when the runtime has no podman client at a fixed path to hand over."""
        runner = self.runtime.runner
        if not isinstance(runner, SubprocessPodman):
            return None
        try:
            podman = runner.path()
        except OSError:
            return None
        argument = {'project': self.runtime.project_id, 'prefix': self.runtime.name_prefix, 'podman': podman,
                    'global': list(runner.global_args), 'name': started.name, 'grace': self.config.grace_seconds,
                    'cgroup': started.cgroup_path or None, 'parent': os.getpid()}
        environment = {name: os.environ[name] for name in PODMAN_ENVIRONMENT if name in os.environ}
        return {'argument': json.dumps(argument, sort_keys=True, separators=(',', ':')), 'environment': environment}

    # -- the one stop action (C3)
    def _stop_record(self, started, reason, on_gone):
        if reason not in CONTAINER_STOP_REASONS:
            raise ValueError('unknown stop reason')
        with self._stops_lock:
            record = self._stops.get(started.token)
            if record is None:
                record = self._stops[started.token] = _Stop(started, reason)
            if on_gone is not None and on_gone not in record.listeners:
                record.listeners.append(on_gone)
            return record

    def stop(self, started, reason='run_ended', *, on_gone=None, now=False):
        """Stop and remove the container; True only when the OS says it is gone.

        One attempt at most, serialized with every other stop of this container: a stop under way
        is waited for; a container proven gone is not stopped again; after a failed stop the next
        attempt waits for its back-off (`retry_stops`) unless `now`. Each attempt writes `stopping`
        (with the first reason) before it, and `gone` or `stop_failed` after it. `on_gone` is
        called once the container is proven gone, now or by a later retry.
        """
        record = self._stop_record(started, reason, on_gone)
        with record.lock:
            if record.gone:
                return True
            if record.attempts and not now and (record.retry_at is None or self._clock() < record.retry_at):
                return False
            gone = self._attempt(record)
        if gone:
            self._proven_gone(record)
        return gone

    def request_stop(self, started, reason, *, on_gone=None):
        """The same stop on a helper thread, for a caller that must not wait (the liveness clock's
        request, made under the execution registry's lock); a Future of its answer."""
        self._stop_record(started, reason, on_gone)
        with self._stops_lock:
            if self._closed:
                future = Future()
                future.set_result(False)
                return future
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=STOP_THREADS, thread_name_prefix='container-stop')
            return self._executor.submit(self.stop, started, reason, on_gone=on_gone)

    def retry_stops(self):
        """Try again every stop whose back-off is over; the number of attempts made."""
        with self._stops_lock:
            due = [record for record in self._stops.values() if not record.gone and record.retry_at is not None
                   and self._clock() >= record.retry_at]
        made = 0
        for record in due:
            with record.lock:
                if record.gone or record.retry_at is None or self._clock() < record.retry_at:
                    continue
                made += 1
                gone = self._attempt(record)
            if gone:
                self._proven_gone(record)
        return made

    def _attempt(self, record):
        """One stop (under the record's lock): `stopping`, the primitive's stop, then its answer."""
        token = record.started.token
        try:
            self.journal.stopping(token, record.reason)
        except JournalError:
            pass  # Stopped all the same; whether it is gone is recorded below if it can be.
        try:
            # C4: only a container that carries this run's own labels is stopped under its name.
            result = self.runtime.stop(record.started.name, grace_seconds=self.config.grace_seconds,
                                       cgroup_path=record.started.cgroup_path or None,
                                       labels={PROJECT_LABEL: self.runtime.project_id, RUN_LABEL: record.started.token})
            gone, detail = result.gone, result.detail
        except Exception as error:  # A primitive fault is never "gone".
            gone, detail = False, f'the stop failed: {type(error).__name__}; hold its slot and tell the owner'
        record.attempts += 1
        try:
            self.journal.stopped(token, gone, detail)
        except JournalError:
            pass  # The OS's answer stands; the entry keeps its last recorded state.
        if gone:
            record.gone, record.retry_at = True, None
        else:
            record.retry_at = self._clock() + stop_retry_delay(record.attempts)
        return gone

    def _proven_gone(self, record):
        with self._stops_lock:
            listeners, record.listeners = list(record.listeners), []
        for listener in listeners:
            try:
                listener()
            except Exception:
                pass  # One caller's failure never hides the proof from the others.

    def close(self):
        """No new helper stops; those under way finish on their own."""
        with self._stops_lock:
            self._closed = True
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)

    def _record_stop(self, token, reason, gone, detail):
        """A stop the primitive already made (a refused launch's cleanup): its reason and answer."""
        try:
            self.journal.stopping(token, reason)
            self.journal.stopped(token, gone, detail)
        except JournalError:
            return False
        return True
