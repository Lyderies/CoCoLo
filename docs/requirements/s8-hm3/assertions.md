# Assertion replacements

The new registry adds HM-3 while preserving the existing self-check and container order.
These are registry expectation changes required by S8-HM3-6, not weakened behaviour checks.

| Test | Old assertion | Replacement | Reason |
| --- | --- | --- | --- |
| test_health_monitor.SweepTests.test_HM_0_5_a_collector_with_only_its_own_checks_is_healthy | checks == [self_check, containers] | checks == [self_check, containers, holds] | Require the new built-in check as well; the healthy findings assertion stays. |
| test_health_containers.CollectorWiringTests.test_HM_1_9_the_collectors_health_monitor_carries_the_containers_check_over_its_state_root | names == [self_check, containers] | names == [self_check, containers, holds] | Require HM-3; the container type, index, root and quiet-pass assertions stay. |
| same test | states == {self_check: ok, containers: ok} | states == {self_check: ok, containers: ok, holds: ok} | Require the additional healthy reading. |

No other existing assertion was removed or changed. New-test setup corrections use the existing
fixture's exported key and result_identity clock floor, supply its project ID to the plan checker,
and create a context before injecting missing custody. All original behaviour assertions remain.
