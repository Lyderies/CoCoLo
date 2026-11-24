# RS-E E2a: launch record, authenticated wake publish, crash-safe wake, reconcile (CoCoLo 0.4.0)

Source: `~/Code/temp/orchestrator-child-liveness/DESIGN-0.4.0-rse-e2.md` draft 2 plus its "Draft 3 corrections"
(primary decision 150), which override draft 2 where they differ. `D:n` is a line of that file. E2a only: nothing is
started here (the starter, budgets, backoff, park and status are E2c; the fenced restart is E2b). Owner decision 143
(crash-loop park) is not needed by E2a. Paths are relative to
`packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.

**E2a-1 — Additive ledger schema.** (D:21, D:35, D:40, D:127) Opening the collector ledger adds, without changing
`PRAGMA user_version` (16), the nullable columns `launch_copy` and `launch_digest` to `projects` and the table
`project_wake` (one row per project: allocated epoch, published generation epoch + seq, state
`pending|exact|clean_stop` or null when only reconciled, next due, a body digest, the collector's `due_at_ms`, a
refused count, the update time) with an index on `due_at_ms` for due rows. Reopening is idempotent. The launch columns are
machine-local: a project backup snapshot keeps its exact earlier format (the six original project columns) and a
restored row has no launch record. Tests: tests.test_e2a_launch_record.LedgerSchemaTests.test_E2a_1_additive_columns_and_wake_table_keep_the_schema_version,
tests.test_e2a_launch_record.LedgerSchemaTests.test_E2a_1_backup_keeps_its_format_and_restore_has_no_launch_record.
Review round 1: also the additive table `project_wake_instances` (per project, the service instance its last epoch was allocated to; machine-local, never in a backup).

**E2a-2 — Registration never writes the launch record.** (D:24, D:127) The registration insert names its columns
explicitly; a newly registered project has a null `launch_copy`, `launch_digest` and no wake row. Test:
tests.test_e2a_launch_record.LedgerSchemaTests.test_E2a_2_registration_names_its_columns_and_writes_no_launch_record.

**E2a-3 — Setup writes a validated launch record.** (D:21-23, D:113-114) `install-codex-rule` (the setup step that
writes the protected copy) validates the launch target and then records `launch_copy` + `launch_digest` for the
project's active row. The target is valid only when the copy sits at
`~/.codex/agent-orchestration/<project digest>/<runtime digest>`, its name is the digest, its binding names this
project and `runtime_copy_problem(copy, root)` is None; where setup ran (`clone_root`) is not consulted. The ledger
refuses a record whose digest is not 64 hex, whose copy is not absolute, or whose copy's name is not the digest or
whose parent is not the project's digest folder, and an unregistered project; a refusal changes nothing. The record goes
through the running collector's new mutating `record_launch` operation (RS-F S1 runtime gate) or, with no collector
running, the ledger directly. Tests: tests.test_e2a_launch_record.LaunchTargetTests.test_E2a_3_a_valid_copy_is_a_launch_target_whatever_clone_ran_setup,
tests.test_e2a_launch_record.LaunchTargetTests.test_E2a_3_a_copy_bound_elsewhere_or_changed_is_not_a_launch_target,
tests.test_e2a_launch_record.LedgerSchemaTests.test_E2a_3_ledger_records_only_a_well_formed_launch_record,
tests.test_e2a_launch_record.CollectorRecordTests.test_E2a_3_record_launch_over_the_collector_wire_and_its_runtime_gate,
tests.test_e2a_launch_record.LaunchTargetTests.test_E2a_3_setup_records_the_launch_target_through_the_collector,
tests.test_e2a_launch_record.LaunchAuthorityTests.test_E2a_3_record_launch_needs_the_owner_and_a_copy_the_collector_verifies,
tests.test_e2a_launch_record.SetupOfflineTests.test_E2a_3_with_no_collector_running_setup_records_in_the_ledger.
Review round 1: `record_launch` is the owner's only: the request carries an `owner-cli-v1` preamble under the key the collector issues at each start (`<state>/collector-owner/owner.cap`, proof written in its owner-only proof folder) naming exactly this request with the project root as subject, checked before anything else; then the collector itself verifies the whole target with `launch_target_problem` (its trusted copy root: the account's `~/.codex/agent-orchestration/<project digest>/<digest>`, bound to this project, byte for byte); any failure is `operation_rejected` and changes nothing. With no collector answering at all, setup records directly in the ledger under exclusive owner maintenance (`CollectorMaintenance`); a collector of another runtime is never stopped for it (`not_recorded`).

**E2a-4 — Copy pinning.** (D:115-116) The registered copy (marked by setup once its launch record is written; one per
project) and any copy held in use (`pin_runtime_copy`, a shared lock a launcher holds from verification until its child
serves) are pinned. `prune_runtime_copies` skips pinned copies; a same-name replacement of a copy in use is refused and
leaves it in place; owner removal leaves an in-use copy in place (and reports no container removed) and otherwise removes
as before. A launcher cannot pin a copy while a prune, replacement or removal holds it. Tests:
tests.test_e2a_launch_record.PinningTests.test_E2a_4_prune_skips_the_registered_and_in_use_copies,
tests.test_e2a_launch_record.PinningTests.test_E2a_4_replacement_and_removal_skip_a_copy_in_use,
tests.test_e2a_launch_record.PinningTests.test_E2a_4_a_pin_waits_for_no_one_and_fails_while_a_prune_holds_the_copy,
tests.test_e2a_launch_record.PinOrderTests.test_E2a_4_setup_pins_the_new_copy_before_the_record_and_clears_the_old_pin_after,
tests.test_e2a_launch_record.PinOrderTests.test_E2a_4_a_copy_pinned_while_a_prune_runs_is_never_removed,
tests.test_e2a_launch_record.PinOrderTests.test_E2a_4_a_pin_on_a_lock_file_a_replacement_dropped_never_lets_a_second_replacement_in,
tests.test_e2a_launch_record.PinOrderTests.test_E2a_4_owner_removal_keeps_the_registered_copy.
Review round 1: the project's stable lock (its copies' folder, flocked exclusive) serializes prune, replacement, removal and the registered marks; a launcher's shared pin checks the lock file it locked is the one the path names (a dropped lock file is never a pin). Setup marks the new copy before its record commits and drops the earlier mark only after; a failed record keeps the earlier mark and drops only one it added; a mark is refused for a copy already gone. Owner removal also leaves the registered copy (and its folder) in place.

**E2a-5 — Publish credential bound to one project.** (D:31-33, D:117-118) `account.publish_wake` is a
PROJECT_OPERATIONS operation. Its credential is a per-project wake key (`client-runtimes/<root digest>.wake.cap`), issued
by the collector for every active project at account-host start and at registration. The verifier picks the key by
the preamble's subject: a `project` preamble whose MAC verifies only under that subject's wake key is marked bound to it;
such a credential may call `account.publish_wake` and nothing else, and `account.publish_wake` is refused for any other
credential. A key for project A presented for B's root, an unknown root, a forged body or a replayed preamble is
refused at enrollment exactly as for an unknown project; a bound credential whose body names another project id, a known
or an unknown one, gets the same `account_not_authorized` before any lookup of the target's wake state; nothing changes.
Tests: tests.test_e2a_wake_wire.WakeWireTests.test_E2a_5_a_credential_is_bound_to_one_project_and_denials_are_uniform,
tests.test_e2a_wake_wire.WakeWireTests.test_E2a_5_the_wake_key_publishes_nothing_else_and_the_account_key_cannot_publish,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_5_the_verifier_probes_no_caller_named_path_and_binds_the_project_id.
Review round 1: the verifier's keys are an in-memory table filled where the keys are issued (account-host start, registration), each bound to the immutable project id it was issued for; a subject with no key is checked against a dummy key, and no caller-named path is touched; a bound credential is authorized by that id alone (no root lookup).

**E2a-6 — Generations: durable epoch, seq, idempotent, stale refused.** (D:34-36, D:121-122) The payload is exactly
`epoch` (null, or an allocated epoch), `seq` (>= 1), `state` and `next_due_at_ms` (null or >= 0). A null epoch allocates
the project's next epoch, durably and strictly increasing across collector restarts. A strictly newer (epoch, seq)
replaces the row; the same generation with the same body is `duplicate` and changes nothing; an older generation, the
same generation with a different body, or an epoch never allocated is refused `wake_stale`, counted, the row otherwise
unchanged. The reply is exactly `{epoch, seq, outcome}`; the new operation is in the B4m reply-shape table, and the
RS-F S1 gate refuses it from another runtime with `runtime_upgrade_required` before authorization, changing nothing.
Tests: tests.test_e2a_wake_wire.WakeWireTests.test_E2a_6_epochs_are_durable_duplicates_are_no_ops_and_stale_is_refused,
tests.test_e2a_wake_wire.WakeLedgerTests.test_E2a_6_ledger_generation_rules,
tests.test_e2a_wake_wire.WakeProtocolTests.test_E2a_6_reply_shape_row_and_closed_reply,
tests.test_e2a_wake_wire.WakeProtocolTests.test_E2a_6_runtime_gate_refuses_an_old_caller_before_authorization,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_6_a_lost_first_acknowledgement_does_not_allocate_a_second_epoch.
Review round 1: a null-epoch payload may also carry `instance` (32 hex, fresh per service instance, MAC-bound); a retry from the instance the last epoch was allocated to gets that epoch again, not a new one.

**E2a-7 — Crash-safe wake from the project service.** (D:37-39, D:119-120) A project service, once its store is open,
publishes `pending` (allocating its epoch) and then `exact` with its store's next due (the earliest `due_at_ms` of a
held or releasing hold). Before any hold write (`take_hold`, or a custody deadline being set) whose deadline is earlier than
the last acknowledged publication's, or before its first hold write while this instance has no acknowledged
publication, it publishes `pending` and waits for the acknowledgement, at most 2 s. After the transaction ends it
publishes `exact` with the next due, from a background sender, never overtaking a later `pending`. With the collector
unreachable the write proceeds and the instance keeps an unpublished flag and retries. Killed between the pending
acknowledgement and the commit, or between the commit and `exact`, the ledger keeps `pending` (unknown, E2a-9).
Tests: tests.test_e2a_wake_wire.WakeServiceTests.test_E2a_7_a_live_service_publishes_pending_then_exact,
tests.test_e2a_wake_wire.WakeServiceTests.test_E2a_7_killed_after_the_pending_ack_or_after_commit_leaves_pending,
tests.test_e2a_wake_wire.WakePublisherTests.test_E2a_7_earlier_deadlines_need_a_pending_ack_and_exact_never_overtakes_it,
tests.test_e2a_wake_wire.WakePublisherTests.test_E2a_7_unreachable_collector_lets_the_write_proceed_flagged,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_7_a_first_write_unpublished_after_a_clean_stop_is_still_reconciled,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_7_a_write_while_pending_invalidates_the_queued_exact,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_7_a_releasing_open_binding_counts_by_its_withdrawal_retry,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_7_a_reply_past_the_limit_is_refused_and_flagged.
Review round 1: (a) a first write with no acknowledged publication (the collector may still hold `clean_stop`) first leaves the durable marker `<state>/projects/<id>/wake-unpublished`, which reconcile reads, and is refused (`history_service_unavailable`) when even that cannot be written; the first acknowledged publication removes it. (b) every hold write invalidates a queued `exact`. (c) the next due also counts a releasing open binding's withdrawal retry (`next_check_ms`), and marking a withdrawal (which moves it earlier) is guarded like `take_hold`. A reply past 16 KiB ends the exchange (flagged, retried).

**E2a-8 — Stop.** (D:42-43, D:123) A normal stop publishes `clean_stop` with the next due after final settlement,
best effort within a 2 s deadline (a collector that accepts and never answers does not delay the stop past it). A
writer-fault stop publishes nothing, so the wake state stays unknown. Tests:
tests.test_e2a_wake_wire.WakeServiceTests.test_E2a_8_a_normal_stop_publishes_clean_stop,
tests.test_e2a_wake_wire.WakePublisherTests.test_E2a_8_clean_stop_is_bounded_and_a_writer_fault_publishes_nothing,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_8_clean_stop_needs_settlement_and_a_final_due_read.
Review round 1: `clean_stop` is attempted only after verified final settlement (every thread stopped and settlement ran to its end) and a successful final read of the store's next due (its own read-only connection), and carries that value, never a cached one.

**E2a-9 — Reconcile marks unknown projects due; nothing starts.** (D:40-41) At collector start (before it reports
ready) and once per sweep interval (on a thread of its own, outside the sweep cycle's ledger hold), each active project with no wake row, a null or `pending` state, or a state other
than `clean_stop`, that no live host serves, is marked due now (`due_at_ms` = now); a `clean_stop` row keeps its
published due. The served check runs off the ledger lock; at most 64 projects per pass; a publication that lands
between the check and the mark is not overwritten. Nothing is started or spawned. Tests:
tests.test_e2a_wake_wire.WakeReconcileTests.test_E2a_9_unknown_projects_are_marked_due_at_collector_start_and_clean_stops_are_not,
tests.test_e2a_wake_wire.WakeReconcileTests.test_E2a_9_a_newer_publication_is_not_overwritten_and_serving_projects_are_skipped,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_9_one_locked_read_examines_at_most_one_page_of_projects,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_9_reconcile_reaches_every_project_past_one_page,
tests.test_e2a_wake_wire.WakeRoundOneTests.test_E2a_9_the_startup_pass_is_bounded_and_the_rest_follows.
Review round 1: a pass pages by rows examined (at most 64 project rows per locked read, by primary key, inactive and settled rows included), page after page from a saved cursor; a `clean_stop` row whose service left the unpublished-write marker counts unknown; the startup pass probes for at most 1 s and the wake thread finishes the rest at once.

**E2a-10 — Setup records the launch target for every project.** (primary decision 171) The setup and update
registration step (`learning-register`, which Codex and Claude-only setups alike run) installs the project's protected
runtime copy, validates it as the launch target (E2a-3) and records it through the running collector, then marks it
registered (E2a-4); no Codex rule is written. It reports `launch_record: recorded` or `not_recorded`; a failure never
fails the registration. `install-codex-rule` keeps recording too. Test:
tests.test_e2a_launch_record.SetupRecordTests.test_E2a_10_registration_records_the_launch_target_without_the_codex_rule.

**E2a-11 — Status for a project with no launch record.** (primary decision 171) `learning-status` shows `recovery:
"recovery pending: run setup to record the install"` for a registered project whose collector row has no launch record
(never recorded, or restored from a backup, which excludes the machine-local launch columns), and null when the record
exists. Test: tests.test_e2a_launch_record.SetupRecordTests.test_E2a_11_status_says_run_setup_when_no_launch_record_exists,
tests.test_e2a_launch_record.LaunchAuthorityTests.test_E2a_11_launch_status_authorizes_its_subject_before_any_lookup.
Review round 1: `launch_status` answers only an owner-signed request (as `record_launch`), checked before any lookup; an unsigned request gets the same refusal for every root. A caller who cannot sign (no line) shows null.