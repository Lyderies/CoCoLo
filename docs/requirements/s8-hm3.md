# S8: HM-3 holds check

Authority: master plan v2 section 1 and S8; supervisor slices HM-3; shared design SH-7;
run-supervisor RS-6. Unit base: ce81bd96 (0.8.67, IP-A). No version or digest catalogue change.

| ID | Observable requirement | Test in test_health_holds |
| --- | --- | --- |
| S8-HM3-1 | Report account register drift against RS-A sources and project register drift against SH-7 sources, including held rows without a source and sources without a hold. Never release or repair rows. | test_account_orphan_is_reported; test_project_orphan_is_reported; test_missing_hold_is_reported |
| S8-HM3-2 | Report account bindings, reserved probes and eligible run slots, plus project holds whose due time and next eligible check are both more than one sweep interval behind effective now. Equality is quiet; a scheduled retry in the future is quiet. Use the persisted clock floor without advancing it. | test_account_due_binding_is_reported_without_release; test_overdue_boundary_and_backoff; test_clock_floor |
| S8-HM3-3 | A service with a faulted result writer is reported before it is stopped, even while it holds work; failed stops remain reported. Healthy services are untouched. Use D13's shutdown and leave settlement to the successor. | test_faulted_service_reported_then_stopped; test_service_stop_failure; test_healthy_service |
| S8-HM3-4 | Check SH-7 custody/context relationships, bytes and prepared publication material with the existing validator predicates. Healthy stores report nothing. | test_custody_drift; test_publication_material_is_checked; test_context_byte_budget_across_pages; test_context_byte_budget_does_not_combine_changed_snapshots; test_healthy_store |
| S8-HM3-5 | Bound source reads with keyset pages through existing indexes and deadlines; use owner read snapshots for the ledger and read-only connections for project histories. Continue after each page; no lifetime refusal, full scan, import-time I/O or writes from checks except service stop. | test_past_page_limit; test_account_projects_past_page_limit; test_past_legacy_evidence_limit; test_query_plans_and_read_only; test_unreadable_account_is_plain_machine_finding; test_deadline |
| S8-HM3-6 | Findings use fixed plain owner wording and project scope, retain findings across project pages and label readings older than one sweep, with no paths, private identities or exception text. Keep with_builtin_checks unchanged; the primary adds the collector registration after S5 (MED-3). | test_scoping_and_private_text; test_registration; test_findings_survive_other_project_pass_and_say_when_stale |

Implementation boundary: every account read runs on the ledger owner through a bounded request. Project
histories use independent read-only snapshots. The project service uses D13 shutdown and retains a fixed
service fault status for the collector to read; a healthy successor acknowledges that status.
No SQL schema, source-record shape, account operation, version, or catalogue changes are authorized here.
The machine-local service-writer-status file contains only one existing fixed finding code, at most 20 bytes;
it is operational status, never a learning source record. It is replaced atomically with owner-only permissions,
read with a 64-byte bound and no symlink following, and removed by healthy startup or project deletion.
Per-step lifecycle, cross-platform, other-provider review and batch full-suite gates remain the primary's responsibility.

Implementation notes: live-register pages use the existing partial due indexes; retained-source pages use
SQLite rowid seeks and bounded primary-key lookups. The legacy deadline evidence query uses records_run;
two matching intents already prove ambiguity, so it reads at most two. Byte totals span pages only while
the database/WAL generation remains stable; a changed generation restarts that aggregate without resetting
relationship checks. Repeated write activity can defer completion of a large aggregate to a stable interval.
No check acquires a project writer, advances a clock floor, adopts a row, releases a hold or repairs a record.


Fix-round requirements (same S8 IDs, review 040-s8-r1):
- HM3-2: overdue observations stay present across every pass with more than 64 live holds; seek the due index directly, bind the project and use its saved result clock floor for account and project checks.
- HM3-3: real serve_project shutdown, including a stop signal arriving first, leaves a durable service finding consumed by HoldsCheck and HealthMonitor status; the successor clears it only after healthy startup.
- HM3-5 / E1b-1: all account statements execute through a bounded ledger-owner request; no ledger reader is opened by the check thread. Project histories remain read-only.
- HM3-6: restore learning_store.py and with_builtin_checks; defer the exact collector registration line to the primary after S5. Preserve unrelated lifetime cap text byte-for-byte and bind project identities.

Fault reporting must still reach status when project history is unreadable, and a report I/O error must not prevent the D13 close attempt.
