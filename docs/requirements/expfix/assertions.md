# expfix changed assertions
- tests/test_legacy_history_compatibility.py test_ordinary_expiry_frees_record_id_and_restarts_empty_project_sequence:
  `"source_sequence": 1` -> `3`. Reason: L10.3 requires learning-expire to leave an event summary; the project is no longer
  empty after expiry (event seq 1, summary seq 2), so the next append is 3. Id reuse and the other assertions are unchanged.
