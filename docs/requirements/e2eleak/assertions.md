# Assertion accounting

No existing assertions are removed or changed. New tests exercise disposable World-like collectors and project services, and retain independent emergency cleanup of only their own recorded subprocesses.

New fixture liveness helper: ps status (blocked by sandbox) -> os.kill(recorded_pid, 0). Survivor and directory assertions remain identical; this strengthens the check by counting unreaped processes as survivors.

Coverage additions preserve every existing assertion: failure modes gain partial construction, and each World exit now also asserts a native detached project service (with no World path in its argv) is dead.

The survivor assertion in the new ownership test is retained verbatim in meaning (`Popen.poll()` must be non-None); its message now includes the recorded and current kernel birth identities for diagnosis. No assertion was weakened to accommodate the observed exit/reap transition.
