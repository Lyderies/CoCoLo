# Assertion replacements

Fix round, review 040-s8-r1, against ce81bd96 (0.8.67). Requirements were extended before the regression tests.

| Test | Reviewed assertion | Replacement | Reason |
| --- | --- | --- | --- |
| test_health_monitor.SweepTests.test_HM_0_5_a_collector_with_only_its_own_checks_is_healthy | checks == [self_check, containers, holds] | checks == [self_check, containers] (original base assertion) | MED-3: primary owns collector registration after S5. Healthy findings assertion preserved. |
| test_health_containers.CollectorWiringTests.test_HM_1_9_the_collectors_health_monitor_carries_the_containers_check_over_its_state_root | names == [self_check, containers, holds] | names == [self_check, containers] (original base assertion) | MED-3: defer collector registration; type, index, root and quiet-pass assertions preserved. |
| same test | states == {self_check: ok, containers: ok, holds: ok} | states == {self_check: ok, containers: ok} (original base assertion) | MED-3: same deferred registration. |
| test_health_holds.HoldsTests.test_registration | with_builtin_checks(checks=(check,)) registers holds | unchanged builtin factory followed by register(check) registers holds | MED-3: restore the original factory API; explicit registration remains tested. |

No other existing assertion was removed or weakened. Final fix: the original real D13 startup test
(test_O10_D13_a_faulted_lane_writer_stops_the_service_and_the_next_one_settles) is restored byte-identical to base
(the fix round had turned it into a delegate that also observed the health monitor). The monitor/status assertions now
live only in two new tests, test_HM3_faulted_service_reaches_health_monitor_status and
test_HM3_stop_signal_first_reaches_health_monitor_status (stop predicate sees the writer fault before finished() does).
Only the writer's expire_results operation is fault-injected; hosts, publisher, collector, report persistence and monitor
are real. Their observer works on a snapshot of the state root, never the live writer lock.

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

## IP-B 0.8.68 wiring (S8-HM3-7)

- No S8 assertion changed. `test_registration_does_not_edit_primary_owned_files_or_builtin_api` stays as it is and passes: the holds check is registered from `health_monitor.py` (`HealthMonitor.register_state_checks`), and `learning_store.py` only calls that method. `with_builtin_checks` is unchanged. The holds check is registered only when the collector has a ledger owner (E1b-1: it can read the account ledger no other way).
- S8-HM3-5 in the full run (docs/batch-0868-full-run.txt) was `test_unrelated_lifetime_caps_bytes_are_preserved` failing against the old byte-prefix rule; the replaced superset rule (docs/batch-0868-merge.md) already fixed it, it is not a wiring gap. It passes in test_health_holds.
