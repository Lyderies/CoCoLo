# lostsettle assertions
No assertion changed or removed.
Setup change (LRS-3): HostRestartTests.test_after_a_host_restart_the_run_is_reported_lost_and_the_primary_can_continue
gains a bounded wait (30 s, polls learning-run-status while reason_code == run_settling) before its unchanged assertions.
New test (LRS-1/LRS-2): HostRestartTests.test_LRS_2_a_fresh_host_still_warming_up_says_run_settling_never_the_generic_refusal.
Setup change (LRS-4, decision 228): tests/test_lost_run_settlement.py `lost_capacity` and `assert_lost_status` now read
status through `run_status`, which polls (bounded 30 s) past run_settling. No assertion changed.
New test (decision 228): HostRestartTests.test_LRS_5_a_failed_first_sweep_keeps_the_host_warming_up (red: _swept true after a failed sweep).
Decision 231: no existing assertion changed. New tests: test_project_driver.WarmUpGateTests (LRS-6 gate per condition, known answers unchanged after both; LRS-7 warm_up report),
HostRestartTests.test_LRS_7_a_failing_first_sweep_is_shown_in_status_with_its_phase_and_fixed_code, test_host_slots.HostWarmUpStatusTests.
New optional key (additive, no exact key-set pin changed): `run_slots.warm_up` inside the capacity report (top-level reply_shapes unchanged, history_api validates the nested key set) and `project_service.warm_up` in learning-status.
Setup change (LRS-7): test_LRS_5 builds its bare driver with the new _warm_failures attribute; assertions unchanged.
