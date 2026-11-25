# E2ELEAK — World process ownership and teardown

**E2ELEAK-1 — Every World ends cleanly.** After pass, assertion failure, exception, step timeout, partial construction failure, interpreter exit or SIGTERM, every process started by that World is stopped and its e2e directory is removed. Detached collectors, project services and clients are included. Cleanup is idempotent and bounded.

**E2ELEAK-2 — Safe recorded ownership.** Persist World identity and process pid plus operating-system start time before running product code. Only matching records belonging to that World may be signalled; pid reuse, another World, malformed records and unrecorded lookalikes must never select a process. No process-name scan is permitted.

**E2ELEAK-3 — Drained stop before escalation.** Teardown first requests the collector's own shutdown_if_drained operation using the World's installed runtime and socket, without starting a collector. Then signal matching recorded survivors with SIGTERM and finally SIGKILL, verifying exit before removing the World directory. Registration closes during teardown so a late child cannot escape.

**E2ELEAK-4 — Runner recovery owns recorded detached processes.** Each worker records its Worlds in its assigned runner folder. On completion, failure, timeout or runner SIGTERM/interruption, the runner reaps these recorded Worlds as well as its worker group, including detached processes outside TMPDIR. A World guardian also handles abrupt owner death. Discovery never scans other e2e directories or touches pre-existing leaked processes. If both runner and guardian are SIGKILLed, or the machine reboots, no next-run scan recovers them; surviving processes and directories require a manual sweep.

Review round 1 corrections (fixed scope):
- E2ELEAK-1: Restore the previous SIGTERM/SIGINT handlers after the last World closes. A guardian wait failure must not mask an active exception or retain a World in the live registry.
- E2ELEAK-2: Spawn and bootstrap registration share the registration flock, allowing concurrent spawns. Closing takes the exclusive flock to wait for in-flight spawners and refuse later ones. Reaped direct children and verified dead recorded processes lose their pid records; live detached children retain theirs. Registry reaping and directory deletion require a resolved `e2e-*` directory directly under the account's resolved `~/.cache` root.
- E2ELEAK-3: Leave registration open throughout drained stop and the TERM wait. Close it immediately before the final survivors/KILL pass. Cache the discovered installed collector client path and do no discovery without a collector socket.
- E2ELEAK-4: Recovery follows only the current runner's recorded Worlds. If both runner and guardian are SIGKILLed, or the machine reboots, no next-run scan recovers them: surviving processes and directories require a manual sweep. No process-name scan is added.
