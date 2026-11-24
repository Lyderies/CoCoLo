# usage-recheck status

State: STOPPED after step 2 + red tests, waiting for an owner decision (USAGE-RECHECK-3 changes RT-RX-5,
owner authority 124.2). UX-HCU-1 is done (independent).

- Analysis: ANALYSIS.md. Requirements: requirements.md (USAGE-RECHECK-1..5, UX-HCU-1).
- Red tests (on purpose, until the decision):
  - tests/test_usage_recheck.py: RecheckDueTests (USAGE-RECHECK-1, missing constants/field),
    EarlyResetTests.test_a_later_reading_of_the_same_window_with_a_new_reset_clears_it (USAGE-RECHECK-3).
    Guard EarlyResetTests.test_other_later_readings_still_do_not_clear_a_measured_reset is green.
  - tests/test_usage_recheck_live.py (real account service): due probe runs while exhausted (-2), route re-checks
    once and an early reset clears Claude (-4/-3/-5), still-exhausted re-check is not repeated (-4/-5) are red;
    the 10-min guard (-2/-5) is green.
- Green: UX-HCU-1 (refusal_steps.py EXACT 'host_client_unknown'); tests.test_refusal_steps unchanged and green.
- Run: PYTHONPATH=<repo>/scripts, TMPDIR=/tmp/claude-501/ur, `$PY -B -m unittest tests.test_usage_recheck
  tests.test_usage_recheck_live tests.test_refusal_steps` from packages/codex-agents.

Implementation plan once decided (option A recommended):
1. admission_metadata.py: constants USAGE_RECHECK_INTERVAL_MS / USAGE_RECHECK_FAR_RESET_MS; classify_provider_state
   adds `recheck_due_at_ms`; explicit-window supersession by a strictly later same-(scope, window_minutes) reading
   with its own different resets_at that rates the window non-restrictive (Claude 'rejected' status follows
   the window when the record's restriction is bound to it).
2. evaluate_admission(probe=True): skip the exhausted gate only when the classifier says a re-check is due.
3. cli.py _route_snapshot (~1286): for each provider with recheck due -> Codex observe_provider_client /
   Claude billing_probe_client (host only), then re-read the snapshot; add availability.recheck audit
   (learning_contracts.validate_route_availability must accept it). A failed re-check never fails the route.
4. learning-status: show recheck_due_at per provider (no probe).
No existing assertion changed so far (assertions.md not needed yet).
