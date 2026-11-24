# expfix status
Done: L10.3 learning-expire summarises (same _group_events/_event_summary_record as sweep_event_retention) the expired,
unlinked raw hook/app_server events it removes, in its transaction. Linked-lineage deletion stays whole (no summary).
Tests: tests/test_event_retention.py (2 new). Changed assertion: see assertions.md.
