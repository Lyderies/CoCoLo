# lostsettle assertions
No assertion changed or removed.
Setup change (LRS-3): HostRestartTests.test_after_a_host_restart_the_run_is_reported_lost_and_the_primary_can_continue
gains a bounded wait (30 s, polls learning-run-status while reason_code == run_settling) before its unchanged assertions.
New test (LRS-1/LRS-2): HostRestartTests.test_LRS_2_a_fresh_host_still_warming_up_says_run_settling_never_the_generic_refusal.
