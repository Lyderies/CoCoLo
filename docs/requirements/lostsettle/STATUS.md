# lostsettle status
Done and committed on fix/lost-run-settling: LRS-1 (driver `_swept` flag; client `_lost_run` answers run_settling), LRS-2 (test_LRS_2_...), LRS-3 (bounded wait in HostRestartTests), docs (service-api-development.md, troubleshooting.md).
Green: test_project_driver, test_lost_run_settlement, test_refusal_steps, test_crashed_driver_unit (146 tests, 351 s).
LRS-4: test_lost_run_settlement lost_capacity (l.103-106) read ['capacity_state'] straight after restart (used at l.163): the same window; the fix returns run_settling (no capacity_state), so it still KeyErrors there. Not changed; needs the primary's decision (poll past run_settling in lost_capacity/assert_lost_status).
