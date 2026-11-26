# CI round 3b: assertions changed

- tests/test_project_start.py, test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client:
  old: both due projects had to answer "serving" by (collector ready + STARTER_CHECK_SECONDS + one tick + 1 s).
  new: both must answer "serving" by (latest start begin line + the start's own timeout, START_TIMEOUT_SECONDS / 8 s).
  The "start begins within one interval" assertion is unchanged (begin lines are written by the starter, not by test polling).
  Reason: the requirement gives a start its own deadline; a host process start (an interpreter plus ledger open) taking
  longer on a slow runner failed the interval deadline once on CI. No constant raised.

Other changes in this round are speed-only (same assertions): test_cli_observation_runtime opts into the
TESTSPEED-3 ended-start shim, test_b2_service_outcome_paths skips building an unused class run,
test_b4t_takeover source scans skip files that cannot hold a site.
