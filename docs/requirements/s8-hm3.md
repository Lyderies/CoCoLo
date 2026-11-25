# S8: HM-3 holds check

Authority: master plan v2 section 1 and S8; supervisor slices HM-3; shared design SH-7;
run-supervisor RS-6. Unit base: ce81bd96 (0.8.67, IP-A). No version or digest catalogue change.

| ID | Observable requirement | Test in test_health_holds |
| --- | --- | --- |
| S8-HM3-1 | Report account register drift against RS-A sources and project register drift against SH-7 sources, including held rows without a source and sources without a hold. Never release or repair rows. | test_account_orphan_is_reported; test_project_orphan_is_reported; test_missing_hold_is_reported |
| S8-HM3-2 | Report account bindings, reserved probes and eligible run slots, plus project holds whose due time and next eligible check are both more than one sweep interval behind effective now. Equality is quiet; a scheduled retry in the future is quiet. Use the persisted clock floor without advancing it. | test_account_due_binding_is_reported_without_release; test_overdue_boundary_and_backoff; test_clock_floor |
| S8-HM3-3 | A service with a faulted result writer is reported before it is stopped, even while it holds work; failed stops remain reported. Healthy services are untouched. Use D13's shutdown and leave settlement to the successor. | test_faulted_service_reported_then_stopped; test_service_stop_failure; test_healthy_service |
| S8-HM3-4 | Check SH-7 custody/context relationships, bytes and prepared publication material with the existing validator predicates. Healthy stores report nothing. | test_custody_drift; test_publication_material_is_checked; test_context_byte_budget_across_pages; test_context_byte_budget_does_not_combine_changed_snapshots; test_healthy_store |
| S8-HM3-5 | Bound source reads with keyset pages through existing indexes, deadlines and read-only SQLite connections. Continue after each page; no lifetime refusal, full scan, import-time I/O or writes from checks except service stop. | test_past_page_limit; test_account_projects_past_page_limit; test_past_legacy_evidence_limit; test_query_plans_and_read_only; test_unreadable_account_is_plain_machine_finding; test_deadline |
| S8-HM3-6 | Findings use fixed plain owner wording and project scope, retain findings across project pages and label readings older than one sweep, with no paths, private identities or exception text. Register the collector check with one line changed in health_monitor.py. | test_scoping_and_private_text; test_registration; test_findings_survive_other_project_pass_and_say_when_stale |

Implementation boundary: the collector reads stores independently of their writers. Lane fault state is local
to the project service, so the service-side part uses its existing D13 stop and writer_fault report path.
No schema, stored record shape, account operation, version, or catalogue changes are authorized here.
Per-step lifecycle, cross-platform, other-provider review and batch full-suite gates remain the primary's responsibility.

Implementation notes: live-register pages use the existing partial due indexes; retained-source pages use
SQLite rowid seeks and bounded primary-key lookups. The legacy deadline evidence query uses records_run;
two matching intents already prove ambiguity, so it reads at most two. Byte totals span pages only while
the database/WAL generation remains stable; a changed generation restarts that aggregate without resetting
relationship checks. Repeated write activity can defer completion of a large aggregate to a stable interval.
No check acquires a project writer, advances a clock floor, adopts a row, releases a hold or repairs a record.
