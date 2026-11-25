# TESTSPEED-3 assertions

No assertion was removed, weakened or edited, and no test was renamed. 32 tests before; 16 + 12 + 4 = 32 after.

Moved (test body byte-for-byte the same; only its module changed, so its registry id changed):

- `ReviewRoundOneDriverTests` (12 tests): `tests.test_account_settlement_driver` -> `tests.test_account_settlement_recovery`.
- `ClosureAuthorityTests` (1 test), `StatusAgreementTests` (2), `LostLifecycleLimitTests` (1):
  `tests.test_account_settlement_driver` -> `tests.test_account_settlement_lifecycle`.
- `scripts/test-requirements.json` (RS-B-B4 and the L14 requirement), `scripts/test-layers.json`,
  `scripts/test-stages.json`, `scripts/test-timings.json` and `test_reply_shape_live.DRIVERS` follow the move.

Changed fixture behaviour (old -> new):

- `tests/test_project_driver.py` `DriverHarness.separate_dispatch`: `python -I -B -S cli.py dispatch ...` ->
  for a class that sets `dispatch_pycache`, `python -I -S -X pycache_prefix=<class cache> cli.py dispatch ...`; the
  dispatch arguments are unchanged; other classes unchanged.
- `tests/test_account_settlement_driver.py` `SettlementHarness`: sets `dispatch_pycache` per class (`setUpClass`),
  and patches `project_driver.SWEEP_SECONDS` 1.0 -> 0.1 in `setUp` (every test of the three modules).
- `@requirements(...)` added (registration only) on `test_B4_1_...`, `test_B4_8_...` and the two `test_c_H3_...`
  second-stop tests; each keeps `RS-B-B4` and gains `TESTSPEED3-*`.
