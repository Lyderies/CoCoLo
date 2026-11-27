# CI round 3b: assertions changed

- Decision 412: tests/test_project_start.py, test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client.
  old: both due projects had to answer "serving" by (collector ready + the TEST's compressed STARTER_CHECK_SECONDS 0.1 s
  + one tick + 1 s) = 1.2 s, below a real host process start on a slow runner. Message "the due projects were not serving
  within one interval".
  new: both must answer "serving" by (collector ready + project_start.STARTER_CHECK_SECONDS as the module defines it,
  5.0 s, the production interval), waiting on the serving answer and ending as soon as both serve. Message: "the due
  projects were not serving within one interval". The begin-within-one-compressed-interval assertion is unchanged.
  START_TIMEOUT is not used.

Other changes in this round are speed-only (same assertions).
