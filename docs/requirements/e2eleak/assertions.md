# Assertion accounting

No existing assertions are removed or changed. New tests exercise disposable World-like collectors and project services, and retain independent emergency cleanup of only their own recorded subprocesses.

New fixture liveness helper: ps status (blocked by sandbox) -> os.kill(recorded_pid, 0). Survivor and directory assertions remain identical; this strengthens the check by counting unreaped processes as survivors.

Coverage additions preserve every existing assertion: failure modes gain partial construction, and each World exit now also asserts a native detached project service (with no World path in its argv) is dead.

The survivor assertion in the new ownership test is retained verbatim in meaning (`Popen.poll()` must be non-None); its message now includes the recorded and current kernel birth identities for diagnosis. No assertion was weakened to accommodate the observed exit/reap transition.

## Review round 1 corrections

No assertion is removed or weakened. Existing fixture layout/assertion paths change from
`<scratch>/e2e-fixture` to `<scratch>/.cache/e2e-fixture`, retaining the same “directory
must disappear” assertion. The reaper's `e2e-owned` and runner's `e2e-detached` fixtures
likewise move under a disposable HOME's `.cache`; matching positive ownership and
nonmatching survivor assertions stay intact. This supplies the expected cache boundary
required by MED6 without granting production deletion outside it.

New witnesses cover HIGH1 concurrency/racing close, HIGH2 drain-time spawning, MED3
wait/poll/dead-record pruning with a live detached positive control, MED4 cached/no-socket
discovery, MED6 outside/prefix/symlink refusal, LOW8 handler restoration and LOW9 original
exception/live-registry preservation. They add assertions to E2ELEAK-1 through -4; no new
requirement IDs or test modules are introduced. MED7 corrects recovery wording only.
