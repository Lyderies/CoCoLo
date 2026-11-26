# 0.8.70: a hold the update left unverified (decision 366)

Authority: OWNER-DECISIONS-0.4.0.md decision 366; DESIGN-0.4.0-B4-takeover.md Amendment A1 (an unverifiable
predecessor record stays unchanged; never force-released).

Live finding (0.8.68): the run the 0.8.67 -> 0.8.68 update ended had records that could not be re-verified, so
adoption skipped it (`refused_unverifiable`) and wrote no `takeover_obligations` row. Its project hold stays
`releasing`, the driver retried its settlement for ever (`history_conflict`), and the holds check reported
`hold_overdue` every pass with advice that led nowhere.

Marker: no new record. A hold is "left unverified by the takeover" when it is `releasing`, has no
`takeover_obligations` row, and belongs to a run listed in an adopted `takeover_runs` journal of this store
(reservation identity = bridge run id; lease identity = run handle:attempt).

- **UH-1:** a hold the takeover left unverified is not reported as `hold_overdue`.
- **UH-2:** it is reported as its own plain finding, `hold_unverified_takeover` ("a run ended by the last update is still
  counted because its records could not be checked; harmless, takes no run slot, will not clear by itself"), with
  what to do (nothing is needed). One finding per project, never one per hold.
- **UH-3:** the driver stops retrying that run's settlement: after the first `history_conflict` it asks the writer
  whether the run is an unverified takeover hold; if so it is remembered and not attempted again by this service. No
  release and no account change is made for it.
- **UH-4:** `docs/troubleshooting.md` "Update hand-over" explains this case in plain words.
- **UH-5:** verified takeover rows, and genuinely overdue holds of any other kind, behave as before.

## Assertions
No existing assertion was changed or removed. New tests: `test_b4t_takeover.ChainedOutboxTests.test_UH_*` (UH-1/2/3/5,
real adoption of the corrupt-original fixture and the real holds check), `test_account_settlement_driver.ConfirmFixSweepTests.test_UH_*`
(UH-3/5, the driver's own sweep). UH-4 is the troubleshooting entry (checked by test_UH_troubleshooting below).

## STATUS
Implemented: `project_takeover.unverified_hold_condition/takeover_left_unverified`, `ProjectHistory.takeover_unverified`
(a settlement step in `ResultPersistenceLane.SETTLEMENT_STEPS`), `RunDriver._takeover_unverified`, `health_holds`
(`hold_unverified_takeover`, overdue excludes it). No schema or record change.
