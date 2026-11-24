# upgfix assertions
No existing assertion changed or removed.

## tests.test_learning_store.LearningStoreTests.test_maintenance_never_stops_a_collector_from_under_a_run (UPG-1)
- Old: reported runtime_version "0.8.13" with operation_invalid on the drained stop -> refused (CollectorError operation_invalid); comment "this version with an older schema is not [stopped]".
- New: "0.8.13" (a run-driving release older than this build) -> the drained stop is repeated once in the earlier collector's bare shape (shutdown(only_if_drained=True, earlier_collector=True)) and the hand-over proceeds. Still refused: this build's own version or newer (new case, PACKAGE_VERSION) and "not-a-version". "0.8.12" unchanged (plain stop).
- Reason: that case is exactly the 0.8.55-style collector that blocked every upgrade (owner-seen failure).
