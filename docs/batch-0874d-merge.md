# Batch 0.8.74 re-cut d (branch merge/batch-0874d)

Base f3ccee78 (0.8.74 batch20, never installed). VERSION stays 0.8.74; the 0.8.74 runtime digest is refreshed in place.
The 0.8.73 record, floor and VERSION are unchanged.

## Merged
- fix/0874-ci4: RS-10-DUE runs one transaction per batch; tests TX-1..3 (test_s13_batch_tx).
- fix/0874-torn-read: decision 417, a torn read on the shared connection is a real defect; the fence epoch check
  (store_fence) now runs under the ledger lock; test TORN-1 (test_torn_read).

## Digest refresh
`runtime-releases.json` 0.8.74: 6429442b... -> 4b6d3051341936fa2017084bb9caa6f3ee1205ee22d533fa638ce1ce27ca9060
(project_history.py and store_fence.py changed; `test_runtime_release` green). `generate_test_metadata.py --catalogue` run twice:
byte-identical, scripts/test-requirements.json unchanged (already carried the new IDs). test-timings.json needed no entry.

## Full run (`--base f3ccee78`, real sockets; docs/batch-0874d-full-run.txt, redacted)
576 units, metadata_errors []. test_synchronized_races PASSED (also 13/13 alone, 20 s). Two failing units, both deterministic
(3 of 3 alone), not edited:
- `test_e1a_evidence.OpenContentionTests.test_exhausted_deferred_publication_restores_next_request_and_owner_pump`:
  expects open_account_busy, gets open_failed (requirement E1a-T3 failed).
- `test_collector_read_scope.HostOpenPublishLockTests.test_the_publish_check_waits_for_the_lock_no_longer_than_its_budget`:
  the lock-holder hook never fires (`holders` empty); the open raises sql_read_unavailable.
Both exercise the open/publish lock path that the torn-read fence-under-lock change touches. Not changed here; need a decision
on whether the code or the test follows requirement E1a.
