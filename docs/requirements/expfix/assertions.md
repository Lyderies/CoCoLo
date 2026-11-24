# expfix changed assertions
- tests/test_legacy_history_compatibility.py test_ordinary_expiry_frees_record_id_and_leaves_an_event_summary_before_the_next_sequence:
  `"source_sequence": 1` -> `3`. Reason: L10.3 requires learning-expire to leave an event summary; the project is no longer
  empty after expiry (event seq 1, summary seq 2), so the next append is 3. Id reuse and the other assertions are unchanged.
- tests/test_learning_store.py test_bulk_hook_expiry_has_bounded_database_work: `assertEqual(1000, ledger.expire(...))` ->
  repeat `expire_report` until `more` is false and assert the removed total is 1000; the progress bound (< 1000) is unchanged.
  Reason: one expire call now summarises at most EXPIRE_SUMMARY_BATCH (500) events (review item 3, bounded work).
- The legacy-history test above is renamed to ..._leaves_an_event_summary_before_the_next_sequence (scripts/test-timings.json key updated).
