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

THE SWITCH: `SETTINGS` below. While it is None (as shipped) nothing here is used and every child is
launched as a process, exactly as before. It is flipped by setting it to a `ContainerLaunchConfig`
(the image and the seccomp profile file) once the remaining container slices exist: the COCOLO
machine (C-VM) and the owner's sign-in test, the image with the clients, COCOLO's own credentials
and the per-run network. Until then the command a child is given is the host's, which a container
cannot run. Recovery of container entries after a restart (C4) and the stop for every reason with
its status (C3) are later slices: until then a container entry is never settled by the process
rule, so its slot stays held for the account's own timed rule.

Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import time

from .container_runtime import (DEFAULT_GRACE_SECONDS, DEFAULT_NAME_PREFIX, MAX_TIMEOUT_SECONDS, ContainerLimits,
                                ContainerRuntime, ContainerSpec, Refusal)
from .launch_journal import JournalError, new_token
from .process_tree import RUN_TAG_VARIABLE


@dataclass(frozen=True)
class ContainerLaunchConfig:
    image: str
    seccomp_profile: str
    limits: ContainerLimits = field(default_factory=ContainerLimits)
    workspace_writable: bool = False
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    name_prefix: str = DEFAULT_NAME_PREFIX


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


class ContainerLauncher:
    def __init__(self, runtime, journal, config, *, attach=None):
        if not isinstance(runtime, ContainerRuntime) or not isinstance(config, ContainerLaunchConfig):
            raise ValueError('a container launcher needs a ContainerRuntime and a ContainerLaunchConfig')
        self.runtime, self.journal, self.config = runtime, journal, config
        # Tests only: the local command that stands in for the attached container output.
        self._attach = attach

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
                self._record_stop(token, result.cleanup.gone, result.cleanup.detail)
            raise ContainerLaunchRefused(result.reason, result.message, result)
        started = StartedContainer(token, result.identity.name, result.container_id, result.cgroup_path)
        try:
            self.journal.started(token, result.container_id, result.cgroup_path)
        except JournalError as error:
            self.stop(started)
            raise ContainerLaunchRefused('record_failed', 'the started container could not be recorded, so it was '
                                         'stopped') from error
        return started

    def attach_arguments(self, started):
        if self._attach is not None:
            return list(self._attach(started))
        return self.runtime.attach_arguments(started.name)

    def stop(self, started):
        """Stop and remove the container; True only when the OS says it is gone and that is recorded."""
        try:
            self.journal.stopping(started.token)
        except JournalError:
            pass  # Stopped all the same; whether it is gone is recorded below if it can be.
        result = self.runtime.stop(started.name, grace_seconds=self.config.grace_seconds,
                                   cgroup_path=started.cgroup_path or None)
        return self._record_stop(started.token, result.gone, result.detail, written=True) and result.gone

    def _record_stop(self, token, gone, detail, *, written=False):
        try:
            if not written:
                self.journal.stopping(token)
            self.journal.stopped(token, gone, detail)
        except JournalError:
            return False
        return True
