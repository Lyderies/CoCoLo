# S14b reader and retention inventory

Audit: literal table and record-family references throughout the shipped Python
package (including schema, backup, restore and generic copiers). References are
listed conservatively: writers and validators are included along with readers.
Dynamic table enumeration is covered by `learning_store._backup_tables`,
`project_backup`, `learning_source_image`, `learning_source_transfer`,
`project_learning_source`, `project_learning_corpus`, and `learning_migration`.
No existing vocabulary or validator is removed.

S14 accepted input: 21 rows, 15,068 backed-up column bytes per open/claim/outcome.
Column bytes below are from that accepted measurement, before SQLite and planned
backup overhead (1.745 * 0.972). All retained families are counted over five years,
not multiplied by a fictional steady-state retention period.

| Family | Columns/run | Decision and necessary readers |
| --- | ---: | --- |
| `sessions` | 117 | Project lifetime. Producer/session binding establishes provenance and permits idempotent bind; backing up and restoring must keep that identity. |
| `run_provenance` | 194 | Project lifetime. Producer/client/session identity is used to reject run reuse and validate results and original-open receipts. |
| `host_run_bindings` | 677 | Existing OP-2 bounded retention for finished project-owned opens. Legacy bindings remain for result/replay authority and are sized as exceptions. Existing readers return open_expired/unknown binding rather than recreating an original run. |
| `launch_claims` | 224 | Project lifetime. Unique handle/run/bridge identity prevents claiming a second time; parent of capacity and receipts. Removing it would allow replay or break coverage. |
| `launch_inputs` | 255 | Project lifetime. Immutable execution configuration is rechecked by claim replay, provenance, results and terminal validation. |
| `launch_observations` | 177 | Project lifetime. Pinned identity observation is rechecked by claim replay and execution/result provenance. |
| `launch_receipts` | 493 | Project lifetime. Request/parameter binding preserves idempotent claims and denials; replay is exact and cannot authorize a second execution. |
| `active_run_capacity` | 528 | Project lifetime. Capacity completion/release/recovery checks require the original claim and release identity; timer expiry must not recreate capacity. |
| `account_open_receipts` | 4861 | Existing OP-2 bounded with finished open/binding. Holds prevent premature deletion; released binding hold is the durable open_expired response. Retention does not inspect or alter receipt contents. |
| `account_open_requests` | 523 | Existing OP-2 bounded with receipt/binding. Pending and denied requests have their own expiry. Request scopes and original-key uniqueness enforce open idempotency while present. |
| `admission_evidence` | 692 | Derived; cascades with its record. Preserve first Claude pass, latest older Claude billing/headroom, live reset windows and every row the existing admission scope still selects. Those are account authority, not disposable history. |
| `run_holds` | 253 | Project lifetime. Released rows are replay fences for purged opens and carry generations, lost/crashed receipts and recovery decisions; removing one can reopen work or erase settlement proof. |
| `records`: provider identity/headroom/billing, lifecycle admission, finished dispatch intent | 4,025 total | At least 90 days, then bounded summarisation. Pin active account evidence and all referenced/signed/file-backed detail. |
| `records`: outcome | 2,741 | Bounded at 90 days when unreferenced, matching the learning window. Keep full linked/attested/file-backed outcomes; summaries retain status counts and content digests. |
| `records`: retention summary | measured by tests | Permanent aggregate counts, original ID digests and observation range; no execution or admission authority. |

Other retained run families (capacity completions/executions, settlement and cleanup
receipts, billing probes and observation journals) already have their own reader
and lifetime rules. They are not silently deleted by this sweep. The accepted
S14 25.6 KB model is an open/claim/outcome model; the real lifecycle integration
check additionally measures every backed-up table in the fixture.

The literal record-family reference index is in [record-readers.md](record-readers.md).

## Record readers

- Admission and provider history: `account_service` capture/scope and signature
  recapture; `learning_store` provider/admission/evidence readers; `attestation_capture`.
  The sweep calls the existing scope and preserves evidence still in effect.
- Dispatch: `learning_store` claim, legacy dispatch, result, provenance, usage,
  crash release and legacy-slot checks; `legacy_slots` process/deadline evidence.
  A binding, open journal or unreleased run slot pins dispatch detail.
- Learning: `learning_store.learning_snapshot`, evaluation and controller readers,
  `learning_engine`, source image/transfer and corpus import. Controlled outcomes,
  controlled cases, promotions, trials, grants, attestations and lineage are untouched.
- History: `ProjectRepository.records` exposes summaries; `.record` resolves an
  old identifier by its digest to its own summary, with explicit aggregate-only
  older-than-90-days text. Unknown identifiers keep the ordinary missing answer.
- Generic copy/read: `validate_learning_record`, `project_learning_corpus`,
  `project_learning_source`, `learning_source_image`, `learning_source_transfer`,
  `project_backup`, `_verify_project_backup_body`. New vocabulary is additive.

## Complete table reference index

### `sessions`

- `account_original_open.py::_managed_open_metadata`
- `account_original_open.py::_receipt`
- `account_original_open.py::finish_open_from_signature_job`
- `host_commands.py::_copy_root`
- `host_commands.py::project_cli`
- `launch_journal.py::_session_members`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_admit_session_run_locked`
- `learning_store.py::_migrate_layout_locked`
- `learning_store.py::bind_session`
- `learning_store.py::delete_project`
- `learning_store.py::open_host_run`
- `learning_store.py::persist`
- `session_guard.py::<schema/layout>`
- `session_guard.py::_sessions_folder`
- `session_guard.py::find_session`
- `session_guard.py::scan_recent_sessions`

### `run_provenance`

- `account_api.py::<schema/layout>`
- `account_api.py::_checked_request`
- `account_api.py::_checked_result`
- `account_api.py::run_provenance`
- `account_host.py::<schema/layout>`
- `account_original_open.py::_managed_open_metadata`
- `account_original_open.py::_receipt`
- `account_original_open.py::current_open_request`
- `account_original_open.py::finish_open_from_signature_job`
- `account_service.py::_handle_request`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_migrate_layout_locked`
- `learning_store.py::commit_host_result`
- `learning_store.py::delete_project`
- `learning_store.py::open_host_run`
- `learning_store.py::persist`
- `learning_store.py::root_bindings`
- `project_driver.py::_advance`
- `reply_shapes.py::<schema/layout>`
- `runtime_files.py::<schema/layout>`

### `host_run_bindings`

- `account_cleanup.py::check_original`
- `account_cleanup.py::release_capacity`
- `account_host_open.py::_snapshot`
- `account_lost_run.py::_known_claim`
- `account_lost_run.py::_run_state`
- `account_lost_run.py::settle_lost_run`
- `account_open_schema.py::assert_open_schema`
- `account_open_schema.py::install_open_schema`
- `account_original_open.py::<schema/layout>`
- `account_original_open.py::_managed_open_metadata`
- `account_original_open.py::_purged`
- `account_original_open.py::_receipt`
- `account_original_open.py::current_open_request`
- `account_original_open.py::finish_open_from_signature_job`
- `account_original_open.py::opened_by`
- `account_original_open.py::original_orchestration_digest`
- `account_original_open.py::purge_finished_opens`
- `account_run_provenance.py::_snapshot`
- `active_run_capacity.py::crashed_claim`
- `learning_source_image.py::<schema/layout>`
- `learning_source_image.py::_drained`
- `learning_store.py::<schema/layout>`
- `learning_store.py::__init__`
- `learning_store.py::_accept_project_terminal`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_claim_context_locked`
- `learning_store.py::_claim_run_rows_locked`
- `learning_store.py::_fence_claim_request_locked`
- `learning_store.py::_initialize_launch_claims_locked`
- `learning_store.py::_initialize_run_holds_locked`
- `learning_store.py::_known_claim_for_request_locked`
- `learning_store.py::_migrate_five_to_current`
- `learning_store.py::_migrate_layout_locked`
- `learning_store.py::_migrate_six_or_seven_to_current`
- `learning_store.py::_new_owned_execution`
- `learning_store.py::_open_run_for_provider_locked`
- `learning_store.py::_operational_qa_scope_locked`
- `learning_store.py::_register_project_execution`
- `learning_store.py::_release_due_locked`
- `learning_store.py::_settle_unlaunched`
- `learning_store.py::account_holds_due`
- `learning_store.py::account_holds_status`
- `learning_store.py::admitted_client`
- `learning_store.py::bind_host_agent`
- `learning_store.py::classify_host_run_abandoned`
- `learning_store.py::close_orchestration`
- `learning_store.py::commit_host_result`
- `learning_store.py::commit_upgrade_reconciliation`
- `learning_store.py::delete_project`
- `learning_store.py::host_owner`
- `learning_store.py::host_run_admission`
- `learning_store.py::ingestion_snapshot`
- `learning_store.py::issue_legacy_fallback_receipt`
- `learning_store.py::mark_learning_moved_if_empty`
- `learning_store.py::metadata`
- `learning_store.py::observe_host_run`
- `learning_store.py::open_host_run`
- `learning_store.py::orchestration_report`
- `learning_store.py::prepare_upgrade_reconciliation`
- `learning_store.py::record_claude_rate_limit_event`
- `learning_store.py::record_dispatch_intent`
- `learning_store.py::record_dispatch_result`
- `learning_store.py::trusted_routing_evidence_refs`
- `learning_store.py::verification_worker_result`
- `learning_store.py::verify_legacy_fallback_receipt`
- `learning_store.py::withdraw_open`
- `run_holds_schema.py::<schema/layout>`
- `run_holds_schema.py::check_run_holds_coverage`
- `run_retention.py::_protected`

### `launch_claims`

- `account_cleanup_schema.py::<schema/layout>`
- `account_original_open.py::current_open_request`
- `active_run_capacity.py::<schema/layout>`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_assert_launch_schema_locked`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_backup_tables`
- `learning_store.py::_capacity_exists`
- `learning_store.py::_check_backup_coverage`
- `learning_store.py::_claim_commit`
- `learning_store.py::_claim_exists`
- `learning_store.py::_initialize_active_capacity_locked`
- `learning_store.py::_initialize_launch_claims_locked`
- `learning_store.py::_migrate`
- `learning_store.py::_migrate_six_or_seven_to_current`
- `learning_store.py::_release_due_locked`
- `learning_store.py::delete_project`
- `learning_store.py::record_dispatch_intent`
- `learning_store.py::withdraw_open`

### `launch_inputs`

- `account_host_open.py::_snapshot`
- `account_original_open.py::_managed_open_metadata`
- `account_original_open.py::_receipt`
- `account_original_open.py::current_open_request`
- `account_original_open.py::finish_open_from_signature_job`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_assert_launch_schema_locked`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_backup_tables`
- `learning_store.py::_claim_context_locked`
- `learning_store.py::_claim_run_rows_locked`
- `learning_store.py::_settle_unlaunched`
- `learning_store.py::delete_project`
- `learning_store.py::open_host_run`

### `launch_observations`

- `account_observation.py::complete`
- `account_observation.py::reserve`
- `account_observation_failure.py::record_observation_failure`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_assert_launch_schema_locked`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_backup_tables`
- `learning_store.py::_claim_context_locked`
- `learning_store.py::_claim_run_rows_locked`
- `learning_store.py::_settle_unlaunched`
- `learning_store.py::delete_project`
- `learning_store.py::observe_provider`

### `launch_receipts`

- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_assert_launch_schema_locked`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_backup_tables`
- `learning_store.py::_known_claim_receipt_locked`
- `learning_store.py::_store_claim_receipt_locked`
- `learning_store.py::delete_project`

### `active_run_capacity`

- `account_cleanup.py::<schema/layout>`
- `account_cleanup.py::release_capacity`
- `account_cleanup_schema.py::<schema/layout>`
- `account_lost_run.py::<schema/layout>`
- `account_lost_run.py::_run_state`
- `account_original_open.py::<schema/layout>`
- `account_original_open.py::current_open_request`
- `account_run_provenance.py::_snapshot`
- `active_run_capacity.py::<schema/layout>`
- `active_run_capacity.py::crashed_claim`
- `learning_source_image.py::<schema/layout>`
- `learning_source_image.py::_drained`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_accept_project_terminal`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_backup_tables`
- `learning_store.py::_capacity_completion_locked`
- `learning_store.py::_capacity_exists`
- `learning_store.py::_capacity_status`
- `learning_store.py::_check_active_capacity_locked`
- `learning_store.py::_check_backup_coverage`
- `learning_store.py::_initialize_active_capacity_locked`
- `learning_store.py::_initialize_run_holds_locked`
- `learning_store.py::_known_capacity_release`
- `learning_store.py::_migrate`
- `learning_store.py::_new_owned_execution`
- `learning_store.py::_record_project_rate_limit_event`
- `learning_store.py::_register_project_execution`
- `learning_store.py::_release_active_capacity`
- `learning_store.py::_release_due_locked`
- `learning_store.py::_release_slot_locked`
- `learning_store.py::_reserve_active_capacity_locked`
- `learning_store.py::account_holds_due`
- `learning_store.py::account_holds_status`
- `learning_store.py::account_slot_health`
- `learning_store.py::commit_owner_record`
- `learning_store.py::delete_project`
- `learning_store.py::earlier_version_runs`
- `learning_store.py::ingestion_snapshot`
- `learning_store.py::machine_idle`
- `learning_store.py::mark_learning_moved_if_empty`
- `learning_store.py::withdraw_open`
- `run_holds_schema.py::<schema/layout>`
- `run_holds_schema.py::check_run_holds_coverage`
- `run_retention.py::_protected`
- `runtime_files.py::<schema/layout>`

### `account_open_receipts`

- `account_open_schema.py::<schema/layout>`
- `account_original_open.py::<schema/layout>`
- `account_original_open.py::_capture`
- `account_original_open.py::_managed_open_metadata`
- `account_original_open.py::_receipt`
- `account_original_open.py::finish_open_from_signature_job`
- `account_original_open.py::opened_by`
- `account_original_open.py::purge_finished_opens`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_backup_tables`
- `learning_store.py::_known_claim_for_request_locked`

### `account_open_requests`

- `account_open_schema.py::<schema/layout>`
- `account_original_open.py::<schema/layout>`
- `account_original_open.py::_capture`
- `account_original_open.py::_managed_open_metadata`
- `account_original_open.py::current_open_request`
- `account_original_open.py::finish_open_from_signature_job`
- `account_original_open.py::preflight_open`
- `account_original_open.py::purge_finished_opens`
- `account_original_open.py::request_journal`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::__init__`
- `learning_store.py::_backup_tables`
- `learning_store.py::_known_claim_for_request_locked`
- `run_retention.py::<schema/layout>`
- `run_retention.py::_protected`

### `admission_evidence`

- `account_service.py::<schema/layout>`
- `admission_evidence_schema.py::<schema/layout>`
- `admission_evidence_schema.py::check_admission_evidence_coverage`
- `learning_source_image.py::_schema`
- `learning_store.py::_other_table_columns`

### `run_holds`

- `account_billing_admission.py::close_expired_reserved`
- `account_billing_admission.py::close_orphaned_admitted`
- `account_cleanup.py::<schema/layout>`
- `account_lost_run.py::_known_claim`
- `account_lost_run.py::_run_state`
- `account_lost_run.py::settle_lost_run`
- `account_original_open.py::<schema/layout>`
- `account_original_open.py::_purged`
- `learning_source_image.py::<schema/layout>`
- `learning_store.py::<schema/layout>`
- `learning_store.py::_backup_expected_columns`
- `learning_store.py::_backup_tables`
- `learning_store.py::_release_due_locked`
- `learning_store.py::_release_slot_locked`
- `learning_store.py::_slot_decision_locked`
- `learning_store.py::account_holds_due`
- `learning_store.py::account_holds_status`
- `learning_store.py::commit_owner_record`
- `learning_store.py::delete_project`
- `learning_store.py::ingestion_snapshot`
- `learning_store.py::machine_idle`
- `learning_store.py::withdraw_open`
- `project_backup.py::<schema/layout>`
- `run_holds_schema.py::<schema/layout>`
- `run_holds_schema.py::_evidence_matches`
- `run_holds_schema.py::check_run_holds_coverage`

