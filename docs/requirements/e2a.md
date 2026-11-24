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
tests.test_e2a_launch_record.LaunchTargetTests.test_E2a_3_setup_records_the_launch_target_through_the_collector.

**E2a-4 — Copy pinning.** (D:115-116) The registered copy (marked by setup once its launch record is written; one per
project) and any copy held in use (`pin_runtime_copy`, a shared lock a launcher holds from verification until its child
serves) are pinned. `prune_runtime_copies` skips pinned copies; a same-name replacement of a copy in use is refused and
leaves it in place; owner removal leaves an in-use copy in place (and reports no container removed) and otherwise removes
as before. A launcher cannot pin a copy while a prune, replacement or removal holds it. Tests:
tests.test_e2a_launch_record.PinningTests.test_E2a_4_prune_skips_the_registered_and_in_use_copies,
tests.test_e2a_launch_record.PinningTests.test_E2a_4_replacement_and_removal_skip_a_copy_in_use,
tests.test_e2a_launch_record.PinningTests.test_E2a_4_a_pin_waits_for_no_one_and_fails_while_a_prune_holds_the_copy.

**E2a-5 — Publish credential bound to one project.** (D:31-33, D:117-118) `account.publish_wake` is a
PROJECT_OPERATIONS operation. Its credential is a per-project wake key (`client-runtimes/<root digest>.wake.cap`), issued
by the collector for every active project at account-host start and at registration. The verifier picks the key by
the preamble's subject: a `project` preamble whose MAC verifies only under that subject's wake key is marked bound to it;
such a credential may call `account.publish_wake` and nothing else, and `account.publish_wake` is refused for any other
credential. A key for project A presented for B's root, an unknown root, a forged body or a replayed preamble is
refused at enrollment exactly as for an unknown project; a bound credential whose body names another project id, a known
or an unknown one, gets the same `account_not_authorized` before any lookup of the target's wake state; nothing changes.
Tests: tests.test_e2a_wake_wire.WakeWireTests.test_E2a_5_a_credential_is_bound_to_one_project_and_denials_are_uniform,
tests.test_e2a_wake_wire.WakeWireTests.test_E2a_5_the_wake_key_publishes_nothing_else_and_the_account_key_cannot_publish.

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
tests.test_e2a_wake_wire.WakeProtocolTests.test_E2a_6_runtime_gate_refuses_an_old_caller_before_authorization.

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
tests.test_e2a_wake_wire.WakePublisherTests.test_E2a_7_unreachable_collector_lets_the_write_proceed_flagged.

**E2a-8 — Stop.** (D:42-43, D:123) A normal stop publishes `clean_stop` with the next due after final settlement,
best effort within a 2 s deadline (a collector that accepts and never answers does not delay the stop past it). A
writer-fault stop publishes nothing, so the wake state stays unknown. Tests:
tests.test_e2a_wake_wire.WakeServiceTests.test_E2a_8_a_normal_stop_publishes_clean_stop,
tests.test_e2a_wake_wire.WakePublisherTests.test_E2a_8_clean_stop_is_bounded_and_a_writer_fault_publishes_nothing.

**E2a-9 — Reconcile marks unknown projects due; nothing starts.** (D:40-41) At collector start (before it reports
ready) and once per sweep interval (on a thread of its own, outside the sweep cycle's ledger hold), each active project with no wake row, a null or `pending` state, or a state other
than `clean_stop`, that no live host serves, is marked due now (`due_at_ms` = now); a `clean_stop` row keeps its
published due. The served check runs off the ledger lock; at most 64 projects per pass; a publication that lands
between the check and the mark is not overwritten. Nothing is started or spawned. Tests:
tests.test_e2a_wake_wire.WakeReconcileTests.test_E2a_9_unknown_projects_are_marked_due_at_collector_start_and_clean_stops_are_not,
tests.test_e2a_wake_wire.WakeReconcileTests.test_E2a_9_a_newer_publication_is_not_overwritten_and_serving_projects_are_skipped.
