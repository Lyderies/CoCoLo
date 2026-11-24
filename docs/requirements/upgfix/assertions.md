# upgfix assertions

## tests.test_learning_store.LearningStoreTests.test_maintenance_never_stops_a_collector_from_under_a_run (UPG-1)
- Old: reported runtime_version "0.8.13" with operation_invalid on the drained stop -> refused (CollectorError operation_invalid); comment "this version with an older schema is not [stopped]".
- New: "0.8.13" (a run-driving release older than this build) -> the drained stop is repeated once in the earlier collector's bare shape (shutdown(only_if_drained=True, earlier_collector=True)) and the hand-over proceeds. Still refused: this build's own version or newer (new case, PACKAGE_VERSION) and "not-a-version". "0.8.12" unchanged (plain stop).
- Reason: that case is exactly the 0.8.55-style collector that blocked every upgrade (owner-seen failure).

## tests.test_rsf_s1_ingress.RepairIngressTests.test_RSF_S1_2_every_collector_mutation_refuses_before_touching_state (UPG-5)
- Old: every mutating op, including shutdown_if_drained, with no digest or a well-formed foreign digest ("0"*64) or a malformed one ([]) was refused runtime_upgrade_required and left `_stop` unset.
- New: the same for every op, except shutdown_if_drained with no digest or a well-formed foreign digest, which is skipped here (accepted by design, UPG-5; proved by test_upgrade_handover SameBuildCollectorTests with a real collector). shutdown_if_drained with a malformed digest ([]) is still refused runtime_upgrade_required.
- Reason: decision 226, the drained stop is how a newer build hands over, so it must not need the serving runtime's digest; nothing else about it changes.

## tests.test_rsf_s1_ingress_socket.RuntimeIngressSocketTests.test_RSF_S1_2_all_mutating_wire_operations_refuse_old_callers (UPG-5)
- Old: every mutating wire op including shutdown_if_drained, with no digest or a foreign well-formed one, answered runtime_upgrade_required and did not stop the server.
- New: shutdown_if_drained is skipped here (accepted by design); all other ops unchanged. Real-process proof: test_upgrade_handover SameBuildCollectorTests.
