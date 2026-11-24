# expfix status
Done: L10.3 + review rounds. expire() drains bounded batches (EXPIRE_SUMMARY_BATCH=500, one lock hold each, page of due ids by LIMIT); CLI and run-cycle use it; collector sweep drains in one pass; one-project event window walks its own index.
unlinked raw hook/app_server events it removes, in its transaction. Linked-lineage deletion stays whole (no summary).
Tests: tests/test_event_retention.py (2 new). Changed assertion: see assertions.md.
