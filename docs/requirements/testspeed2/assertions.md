# TESTSPEED-2 assertions

No assertion was removed, weakened or edited, and no test was renamed.

Changed fixture behaviour (old -> new):

- `tests/wizard_direct_driver.py`, only for a class that sets `shared_pycache = True` (`SetupStraightTests`):
  isolated interpreters the wizard and installer start run `-X pycache_prefix=<class cache>` without `-B`, instead of
  `-B -X pycache_prefix=/dev/null`; the arguments logged to `python.log`, `python-args.log`, `python-full.log` and
  `register.log` are still the wizard's own (with `/dev/null`), so the regex in
  `test_S4_2_set_up_asks_before_moving_earlier_records` is unchanged. `getconf` answers are reused within the run.
  Classes that do not set the attribute are unchanged.
- `tests/test_setup_straight.py`: `from test_framework import requirements` and `@requirements(...)` on three
  tests (registration only); `shared_pycache = True`.
- `packages/codex-agents/tests/test_lost_run_settlement.py`: `@requirements` on the part B test (registration only;
  its body is part B's, untouched).
