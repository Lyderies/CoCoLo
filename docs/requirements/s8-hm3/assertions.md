# Assertion replacements

Fix round, review 040-s8-r1, against ce81bd96 (0.8.67). Requirements were extended before the regression tests.

| Test | Reviewed assertion | Replacement | Reason |
| --- | --- | --- | --- |
| test_health_monitor.SweepTests.test_HM_0_5_a_collector_with_only_its_own_checks_is_healthy | checks == [self_check, containers, holds] | checks == [self_check, containers] (original base assertion) | MED-3: primary owns collector registration after S5. Healthy findings assertion preserved. |
| test_health_containers.CollectorWiringTests.test_HM_1_9_the_collectors_health_monitor_carries_the_containers_check_over_its_state_root | names == [self_check, containers, holds] | names == [self_check, containers] (original base assertion) | MED-3: defer collector registration; type, index, root and quiet-pass assertions preserved. |
| same test | states == {self_check: ok, containers: ok, holds: ok} | states == {self_check: ok, containers: ok} (original base assertion) | MED-3: same deferred registration. |
| test_health_holds.HoldsTests.test_registration | with_builtin_checks(checks=(check,)) registers holds | unchanged builtin factory followed by register(check) registers holds | MED-3: restore the original factory API; explicit registration remains tested. |

No other existing assertion was removed or weakened. The original real D13 startup test now delegates to a shared
helper; every stop, successor-settlement, hint and capability assertion remains, with added monitor/status assertions.
A second test exercises that same real wiring when the stop predicate sees the writer fault before finished() does.
Only the writer's expire_results operation is fault-injected; hosts, publisher, collector, report persistence and monitor
are real. These socket tests cannot get past fixture setup in this sandbox.

Existing runtime fixtures now attach a real LedgerOwner to their existing ledger. New-test fixture corrections supply
unique binding run IDs, a ResultCapacity and private project directories. An initially assumed queue counter did not
exist; the replacement asserts the actual queue refusal map is empty, alongside the SQL thread trace and closed
transaction assertion. No product behaviour assertion was changed to fit an implementation.

New regression assertions: overdue present on every pass past 64 live rows; account project scope; saved project clock
floor applied to account overdue without advancing it; all account SQL on the real owner thread with no independent
ledger reader; parameter-bound project identities; primary-owned files unchanged from base; unrelated cap prefix
byte-identical; durable fault and failed-stop findings survive a fresh check and reach scoped monitor status, then clear
on healthy recovery. The registration ownership guard is builder-only: the primary must update it when adding its merge
registration line, and restore the two collector expectation sets above to include holds.

The existing decision-179 compatibility test in test_e2a_wake_wire previously suppressed _owned_directory
while stubbing its host. That setup now uses the real directory creation so the real report sink can write.
Its writer_fault, faulted wake and no-final-due assertions remain unchanged. This stub test supplements,
and does not replace, the real-service normal and stop-first tests.

Two added MED-2 safety regressions were red before their fixes: a durable service fault must survive an
unreadable project history, and a report sink I/O error must still attempt host.close. The latter's mock
host tests close ordering only; the real-service tests remain the wiring proof required from the primary.
