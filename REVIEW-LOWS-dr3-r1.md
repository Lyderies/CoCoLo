# Review LOWs, DR-3/RC-3 r1

- RC-3 slot-released check (test_result_lifetime.py test_RC_3...) is vacuous: it asserts zero unreleased
  active_run_capacity rows, which also holds when no slot was ever taken. Test stays skipped (names RS-B);
  make it meaningful (assert a slot row existed and is now released) when RS-B builds the driver call.

## From confirming review rev040dr3r2 (2026-11-22, verifier confirmed)
- test_dispatch_wait.py:152-153 comment says per-client while the check is per client run; reword.
