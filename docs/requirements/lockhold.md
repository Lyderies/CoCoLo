# LOCKHOLD — bounded ledger lock work

Base: `51623740` (0.8.65 plus TESTSPEED-3). Authority: E0b-v2 synthetic-ledger
measurements and owner decision 304. VERSION and the runtime digest catalogue
are outside this unit.

| Requirement | Acceptance | Tests |
| --- | --- | --- |
| LOCKHOLD-1 | A collector sweep cycle processes at most the existing 500-event batch under one ledger hold. Each batch commits its summaries and removals together. A larger backlog resumes automatically across separate cycles/holds, with a scheduling gap, and survives stop/restart or rollback. Draining produces the same records and summaries as the former full drain with the same batch boundaries. | `test_lockhold.LockHoldRetentionTests.test_LOCKHOLD_1_backlog_drains_across_bounded_holds_with_identical_final_state`, `test_LOCKHOLD_1_failed_batch_rolls_back_and_restarts`, `test_LOCKHOLD_1_loop_resumes_batches_and_stop_cancels_the_gap`; existing weighted retention, lineage, malformed-group and summary tests in `test_event_retention` |
| LOCKHOLD-2 | With provider evidence fixed, `provider_attestation_status` and health with a project use flat SQLite VM work as unrelated project events grow. A former-scan negative control must grow. Answers, evidence ordering, account matching and current-owner signature verification remain identical. | `test_lockhold.LockHoldReadTests.test_LOCKHOLD_2_status_and_project_health_have_flat_vm_work`, `test_LOCKHOLD_2_status_answers_match_former_lookup` |
| LOCKHOLD-3 | Measure the admission evaluation inside `open_host_run` against growing unrelated history. If it has the same full-scan cause, use selective existing indexes and preserve decisions, including the write-time recheck. If no cheap bounded lookup exists, record the cause and leave that path unchanged. | `test_lockhold.LockHoldReadTests.test_LOCKHOLD_3_open_admission_has_flat_vm_work_and_same_decision` |

Retention policy remains weighted by event code (7/30/90 days in the installed
policy); existing expiries remain authoritative. Only unreferenced hook/app-server
events are swept. Summaries retain the existing per-run/day grouping within each
batch, deterministic identity, counts, source counts, time span and 90-day expiry.
Host events, linked/file-named/granted/attested rows and backups retain their
existing treatment. The owner's `expire()` still drains its backlog in one call,
using separate bounded holds.

Use existing indexes where sufficient. Any additional index must be additive,
recreated on normal open without a schema version change, and removed by genuine
old-layout fixtures. Register count limits and prove work beyond each limit can
finish. Suite fixtures stay small; large measurements belong in `scripts/`.
