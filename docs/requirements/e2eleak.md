# E2ELEAK — World process ownership and teardown

**E2ELEAK-1 — Every World ends cleanly.** After pass, assertion failure, exception, step timeout, partial construction failure, interpreter exit or SIGTERM, every process started by that World is stopped and its e2e directory is removed. Detached collectors, project services and clients are included. Cleanup is idempotent and bounded.

**E2ELEAK-2 — Safe recorded ownership.** Persist World identity and process pid plus operating-system start time before running product code. Only matching records belonging to that World may be signalled; pid reuse, another World, malformed records and unrecorded lookalikes must never select a process. No process-name scan is permitted.

**E2ELEAK-3 — Drained stop before escalation.** Teardown first requests the collector's own shutdown_if_drained operation using the World's installed runtime and socket, without starting a collector. Then signal matching recorded survivors with SIGTERM and finally SIGKILL, verifying exit before removing the World directory. Registration closes during teardown so a late child cannot escape.

**E2ELEAK-4 — Runner recovery crosses sessions and cache roots.** Each worker records its Worlds in its assigned runner folder. On completion, failure, timeout or runner SIGTERM/interruption, the runner reaps these recorded Worlds as well as its worker group, including detached processes outside TMPDIR. A World guardian also handles abrupt owner death. Discovery never scans other e2e directories or touches pre-existing leaked processes.
