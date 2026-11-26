# B4t assertion replacements

All pre-existing assertion changes must be paired below against the unit base `ce81bd96`.

Test-fixture corrections before product implementation: T-15's bound lease was
incorrectly expected to be `claim_pending`; it now expects `bound`, the state
written by the archived fixture's `bind_result_lease`. T-18's recorder-table
name was corrected from nonexistent `project_outcome_recorders` to
`project_result_recorders`. Journal fixture handles now use the actual `hrn_`
shape. These do not weaken the adoption assertions.

T-25 fixture completion: before implementing obligation adoption, added its
authentic launch-journal entry using `LaunchJournal.claimed`; the T4 verifier
must have the original payload inputs specified by H3b. No assertion removed.
The new archive driver captures the archived writer's pending attestation before
bind, as its real driver does. T-25 now consumes those fixture bytes instead of
trying to attest launch readiness from a reopened historical bound lease.

Required replacements before the wizard/lifecycle changes: UPDFIX's running-child refusal
becomes successful hand-over, lost settlement, retained committed results and phase recovery.
Every remaining preflight refusal retains its unchanged-files/rows/processes assertions.

## Additive schema expectation replacements

| Test | Old assertion/stimulus | Replacement | Accepted reason |
|---|---|---|---|
| `AccountObservationBackupTests.test_both_verifiers_preserve_committed_and_unknown_original_requests` | backup schema equals 17 | equals 18; observation original rows/counts still exact | H1d ledger 17→18; new authority tables are omitted, observation data is unchanged |
| `LearningSourceImageTests.test_schema_version_columns_and_primary_key_are_exact` | setting schema to 18 must refuse | setting schema to 19 must refuse | H1d makes 18 the known current ledger; unknown/future schema still refuses |
| `HoldSchemaTests` schema migration assertion in `test_project_holds_schema` | published project schema equals 22 | equals 23 | H1d project 22→23; all existing row comparisons stay |
| `LearningSourceTransferTests.target_rows` (used by both schema-14 fixtures and row comparisons) | enumerate every project table except `runtime_takeover` | also exclude `runtime_floor` and the three takeover tables | H1d/RSF-S2-6: machine-local authority cannot travel into a historical source image; partition row comparisons stay exact |
| `SchemaStepTests.test_X5_M3_one_step_creates_the_four_tables_adopts_nothing_and_both_validators_pass` | original-row comparison omits only schema-18 tables and the epoch | also omit new B4t authority; new schema authority is checked separately | H1d adds machine-local tables without rewriting original rows |

Legacy layout fixture repairs drop new local tables before setting an old schema
number. No historical fixture may retain a floor, lineage or takeover table.
These are fixture construction changes, without removed assertions.

T-18 strengthening: replaced its empty recorder-table comparison across opens
with actual service→primary, primary→service and archived-old→new→primary
recording orders. Each asserts one recorder, unchanged records and recorder
bytes on restart/retry, and one takeover record. T-3 and T-18's primary recheck
also gained actual admission/retrieval and mismatched-terminal refusal checks.

| Test | Old | Replacement | Reason |
|---|---|---|---|
| `LaunchReceiptsLayoutTests.test_receipts_ddl_changes_only_with_a_schema_bump` | ledger schema 17, schema-15/17 receipt hashes | ledger schema 18, same old hashes plus identical schema-18 receipt hash | H1d is additive; receipt DDL and old pins stay unchanged |
| `MigrationCliTests.test_schema15_store_gains_the_cutover_table_only_by_explicit_upgrade` | published project schema 22 | 23; cutover-table emptiness and original sequence assertions unchanged | accepted additive project step |
| `FenceTests.test_epoch_is_inside_the_schema_step_before_each_legacy_adoption_and_rolls_back_with_it` | one adoption callback, final 17/22 | one before-cut callback or two successful stage callbacks, each with exactly the same epoch; final 18/23 | D2.6 MED 2 mandates predecessor publication followed by B4t; the original epoch/rollback assertions remain |

The fence fixture's callback forwards the new stage target keyword, and its
schema-14 ledger drops floor/lineage as well as the epoch. No epoch gate is mocked out.

`SchemaStepTests.test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations`:
the historical-table equality previously followed removal of the epoch alone.
It now additionally checks/removes the new floor (exactly this build's version)
and three empty takeover tables before the same byte-for-byte historical equality.

The following schema/copy assertions retain their original cardinality and byte checks; only the accepted target number changes:

| File / test or helper | Old | New |
|---|---|---|
| `test_rsf_s3_resumable.py::interrupted_equivalence` | `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-22.*'))))` | `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-23.*'))))` |
| `test_rsf_s3_resumable.py::final_boundary_equivalence` | `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-22.*'))))` | `self.assertEqual(1, len(list((root / 'history').glob('history.sqlite3.before-23.*'))))` |
| `test_rsf_s3_resumable.py::test_migration_context_is_reset_after_adoption_and_validation_exceptions` | `self.assertEqual(1, len(list((root / 'history').glob('.history.sqlite3.upgrade-22.*.attempt'))))` | `self.assertEqual(1, len(list((root / 'history').glob('.history.sqlite3.upgrade-23.*.attempt'))))` |
| `test_rsf_s3_resumable.py::test_migration_context_is_reset_after_adoption_and_validation_exceptions` | `(root / 'history').glob('history.sqlite3.before-22.*')]` | `(root / 'history').glob('history.sqlite3.before-23.*')]` |
| `test_rsf_s3_resumable.py::test_project_resume_refuses_missing_kept_before_copy_without_writes` | `marker, = directory.glob('.history.sqlite3.upgrade-22.*.attempt')` | `marker, = directory.glob('.history.sqlite3.upgrade-23.*.attempt')` |
| `test_rsf_s3_resumable.py::test_project_resume_refuses_missing_kept_before_copy_without_writes` | `decoy = directory / 'history.sqlite3.before-22.1'` | `decoy = directory / 'history.sqlite3.before-23.1'` |
| `test_rsf_s3_resumable.py::test_project_resume_refuses_missing_kept_before_copy_without_writes` | `original_marker, = (source / 'history').glob('.history.sqlite3.upgrade-22.*.attempt')` | `original_marker, = (source / 'history').glob('.history.sqlite3.upgrade-23.*.attempt')` |
| `test_project_holds_schema.py::test_D111_a_failing_validator_in_the_18_to_19_step_leaves_18_and_removes_its_copy` | `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)` | `self.assertEqual(23, self.version())  # B6: schema 21 (decision 131)` |
| `test_project_holds_schema.py::test_X5_M2_a_17_binary_refuses_an_18_store_and_changes_nothing` | `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)` | `self.assertEqual(23, self.version())  # B6: schema 21 (decision 131)` |
| `test_project_holds_schema.py::test_M2_an_interrupted_step_rolls_back_to_17_and_a_later_attempt_upgrades` | `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)` | `self.assertEqual(23, self.version())  # B6: schema 21 (decision 131)` |
| `test_project_holds_schema.py::test_M2_an_old_writer_still_running_excludes_the_step` | `self.assertEqual(22, self.version())  # B6: schema 21 (decision 131)` | `self.assertEqual(23, self.version())  # B6: schema 21 (decision 131)` |
| `test_project_holds_schema.py::test_M2_the_copy_keeps_the_17_store_as_it_was` | `self.assertTrue(copy.name.startswith('history.sqlite3.before-22.'))  # B6: schema 21 (decision 131)` | `self.assertTrue(copy.name.startswith('history.sqlite3.before-23.'))  # B6: schema 21 (decision 131)` |
| `test_backup_installed_project.py::test_exact_version_manifests_match_owner_reader_and_keep_old_tables_absent` | `for version in (0, 4, 18):  # PC-4 (requirement): 16 is now supported` | `for version in (0, 4, 19):  # PC-4 (requirement): 16 is now supported` |
| `test_rsf_s2_regressions.py::test_schema16_registration_gate_waits_for_writer_then_upgrades_without_record_consent` | `self.assertEqual(17, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `test_rsf_s2_regressions.py::test_schema16_explicit_upgrade_does_not_request_project_backups` | `self.assertEqual(17, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(18, ledger._connection.execute('PRAGMA user_version').fetchone()[0])` |

B6 current-target pins below move with H1d. The deliberately historical/lookalike before-22 files and stale-marker fixtures retain their original names and preservation assertions.

| Test/helper | Old | Replacement |
|---|---|---|
| `test_b6_unattended_close.py::test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations` | `self.assertEqual(22, db.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, db.execute('PRAGMA user_version').fetchone()[0])` |
| `test_b6_unattended_close.py::test_B6_7_a_real_step_20_store_upgrades_its_records_unchanged_and_adopts_its_open_orchestrations` | `self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-22.'))` | `self.assertTrue(self.copies()[0].name.startswith('history.sqlite3.before-23.'))` |
| `test_b6_unattended_close.py::test_B6_7_a_failure_inside_the_step_leaves_20_and_removes_its_copy_then_a_later_attempt_upgrades` | `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `test_b6_unattended_close.py::test_B6_7_process_death_inside_the_step_leaves_20_and_a_later_attempt_upgrades` | `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `test_b6_unattended_close.py::test_B6_7_an_unknown_object_in_a_step_20_store_is_refused_before_any_write` | `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `test_b6_unattended_close.py::test_B6_7_a_stale_marker_after_inode_reuse_leaves_a_copy_it_did_not_make_alone` | `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `test_b6_unattended_close.py::test_B6_7_a_dead_attempts_copy_with_another_hard_link_is_left_alone` | `(marker,) = self.directory.glob('.history.sqlite3.upgrade-22.*.attempt')` | `(marker,) = self.directory.glob('.history.sqlite3.upgrade-23.*.attempt')` |
| `test_b6_unattended_close.py::test_B6_7_a_dead_attempts_copy_with_another_hard_link_is_left_alone` | `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |
| `test_b6_unattended_close.py::test_B6_7_a_crash_before_the_marker_leaves_no_partial_after_the_next_attempt` | `self.assertEqual(22, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` | `self.assertEqual(23, self.upgrade()._connection.execute('PRAGMA user_version').fetchone()[0])` |

## Accepted behavior replacements

D2.11 retires the interim runtime-only close: open and paused orchestrations,
including unknown/newer scopes, remain in custody. The R10, RS-D N1/N2, B6 and
B7 replacements assert no update-only close/release, zero legacy update-close
counts, and unchanged ordinary retention/primary/idle close behavior. The
recorded RS-D fixture now uses its original sealed client version for an explicit
primary close; a still-open orchestration keeps its tombstone until that close.
The initial B6 held-evidence expectation was corrected to `None`, the actual
pre-existing open-hold shape; no authority or release check was loosened.

UPDFIX wizard refusal fixtures now use `predecessor_unverified`, which remains a
write-free refusal, in place of the retired `child_run_active`. Their file/Git,
remembered-project and no-installer assertions remain. The new busy-update test
requires all three takeover actions in order, automatic lost-run wording and no
idle/manual-stop advice. The former later-child partial-install test is replaced
by a resumable `service_stop_failed` before installer writes, with phase, rerun
command and no claim that nothing changed.

The [exact base audit](assertion-audit.md) pairs all 143 removed or changed
assertion expressions against `ce81bd96`, including renamed functions. The audit
also covers L12.3, G6 and the post-hand-over installer failure in L11.2.
Schema expectations are 18/23; no original receipt or body comparison is removed.
The four additional account schema/backup modules keep their original byte,
rollback and manifest comparisons and move only their published target 17→18.

For L11.2, managed-file and Git rollback remains exact. The collector's original
records and table counts remain exact; only additive local authority is allowed.
The old project rows remain exact except `runtime_takeover`, the deliberately
changed writer epoch. Integrity and the monotonic clock-floor check remain.
The journal must say `account_done`, the predecessor service must be gone, and
the new collector must own the records. The next update keeps that collector
and finishes installation. The new helper's negative tests reject changes to
any original outcome or hold row. This is D2.7's explicit replacement for the
old assertion that processes and both stores all stay on the predecessor after
an installer failure.

The old G6 fault exception (normal and fault runs both red) now always fails:
every affected fault must have a green-to-red transition. The busy scene itself
is judged, rather than a retry after a child finishes. The predecessor fixture
is the unmodified 0.8.66 archive, replacing the synthetic current-code relabel.

New-test fixture corrections after the initial red run: archive execution uses
an unrelated working directory so the current package cannot shadow it;
migration reference and crash workers share the same frozen clock. The native
argument proof now reads the kernel's declared argument-buffer bound (the
fixed maximum buffer was rejected on this macOS release). The real native test
still fails safely when this sandbox denies opener inspection; it is not skipped.
The chain closure fixture serializes the product's returned closure object as
canonical bytes and uses the actual `project_account_settlements` table name.
The missing-lock memory witness preserves all custody/source fields while
allowing, and checking, the existing `instance_unproven` diagnostic annotation;
it does not require a diagnostic reason to remain `cleanup_unknown`.

T-28 now exercises current host authorization and the real history handler,
including enrollment revocation between authorization and commit. Its current
envelope carries an unchanged old result reference; B2's writer predicate still
requires that result's own verified takeover record. T-16's additional archived
SIGKILL witnesses use the real current lifecycle handler and host authorizer:
an existing old scope is usable only with its sealed checkpoint, stored host
class and takeover chain; fresh old-runtime opens remain refused. These add
coverage to the original scaffolds, without deleting their assertions.

## Extended schema and enumeration pass

The additional project/current-ledger schema pins move 22→23 and 17→18. The
original rows, signatures, contexts, body windows, rollback versions and
future-schema refusals remain exact. `test_b2_service_outcome` and
`test_history_service_upgrade` now look for the current `before-23` copy instead
of `before-22`; their copy count, collision/impostor ownership and removal
assertions remain. These helper/stimulus changes supplement the exact changed
assertion expressions in the audit.

`test_learning_store`'s schema-15 and schema-1 fixtures and `test_run_holds`'s
schema-14 fixture now retire the current test fence and drop only the new
machine-local authority before applying their historical layouts. The shared
fixture helper refuses a nonempty lineage; it cannot discard adopted custody.
No original account record or receipt is deleted by that authority cleanup.

The existing hook-aggregate recovery tests exposed that ordinary schema-18
opens had omitted the predecessor's additive layout repair. The product again
backfills a missing aggregate and reconciles an incomplete marker on owner
open, keeping the published schema at 18. Their existing event counts, marker
count and atomic-reconciliation assertions are retained. Both tests were red
before this fix; the only assertion replacement is the accepted 17→18 pin.

Additional current-owner and chain-prefix negatives prevent a result from
borrowing an unrelated/noncurrent takeover record and prevent an annotation
from borrowing an unrelated takeover ID. Chained status counts a bridge run
once even when B and C both list it. Their focused tests were red before the
corresponding fixes and now pass. The outbox-corruption fixture explicitly
commits its negative tamper and compares against an intact control, so its
warning delta is not confused with independently unverifiable contexts.


## Fix round — exact pairs against 44f16529 / 146955fc

The earlier base pairs remain historical. These pairs supersede the earlier sole-holder, J6a, journal-refusal and harness descriptions. Assertions not listed in these changed methods are retained. New witnesses are additional obligations, not replacements for base safeguards.

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store

Replacement: `StoreTests.test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store`. MED-1/T-4: fake flag replaced by archived consumed run, real publish at stop, actual successor classification and recording authority. Mutation removing classification is red.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertTrue(self.ops.kept_result)` | `self.assertEqual(0, made.returncode, made.stderr)`<br>`self.assertEqual(0, done.returncode, done.stderr)`<br>`self.assertIsNotNone(self.store.result_reservation(self.running).committed_at_ms)`<br>`self.assertEqual(0, stopped_children(self.store, self.intent['runs']))`<br>`self.assertIsNotNone(self.store.read_result(self.running))`<br>`self.assertIsNone(db.execute('SELECT committed_at_ms FROM result_reservations WHERE claim_id=?', (self.running.claim_id,)).fetchone()[0])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_4_another_projects_child_keeps_running

Replacement: `AccountLineageTests.test_B4t_4_another_projects_child_keeps_running`. MED-1/T-17: constant replaced by a live subprocess, authentic Q claim, real P account owner adoption, exact Q row equality, diagnostic and Q settlement. Mutating Q custody during P adoption is red.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertTrue(self.ops.other_child)`<br>`self.assertNotIn('other_project_stop', self.ops.events)` | `self.assertEqual('ready', child.stdout.readline().strip())`<br>`self.assertTrue(before)`<br>`self.assertEqual(before, after)`<br>`self.assertIsNone(child.poll())`<br>`self.assertEqual((1, 1), (report['other_projects'], report['other_runs']))`<br>`self.assertEqual('0.8.66', report['updates'][0]['version'])`<br>`self.assertEqual(1, report['updates'][0]['children'])`<br>`self.assertIsNotNone(self.ledger._connection.execute('SELECT released_at_ms FROM active_run_capacity WHERE project_id=?', (self.project,)).fetchone()[0])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_security_takeover_has_no_writer_of_its_own

Replacement: `JournalTests.test_B4t_security_takeover_has_no_writer_of_its_own`. LOW-3: the same writer prohibition covers all five coordinator/OS modules. Preflight is the sole SQL exception and each of its two SQLite connections must explicitly be mode=ro and query_only=ON.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertFalse(any(('sqlite' in name for name in imports)))` | `self.assertEqual(2, len(sql))`<br>`self.assertEqual(2, source.count('PRAGMA query_only=ON'))`<br>`self.assertFalse(any(('sqlite' in name for name in imports)), module.__name__)`<br>`self.assertIn('?mode=ro', ast.get_source_segment(source, node))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_B4t_4_runs_admitted_after_the_intent_are_ended_by_update

Replacement: `JournalTests.test_B4t_4_runs_admitted_after_the_intent_are_ended_by_update`. A1.12 T-35: all three reads select unsettled entries; the same four run identities and restart equality remain. Settled old/late tombstones are separately excluded by the real OwnerOperations reconciliation test.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual(2, self.ops.events.count(('snapshot', False)))`<br>`self.assertEqual(1, self.ops.events.count(('snapshot', True)))` | `self.assertEqual(3, self.ops.events.count(('snapshot', False)))`<br>`self.assertEqual(0, self.ops.events.count(('snapshot', True)))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: JournalTests.test_journal_past_the_run_and_byte_limits_changes_nothing

Replacement: `JournalTests.test_journal_past_the_run_and_byte_limits_changes_nothing`. A1.11 supersedes the old refusal at 1,000 runs: the update must finish, list 64, count 936, and stay <=16,384 bytes. The independent oversized-file and 1,025-byte authorization refusals remain unchanged.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual([], list(self.root.iterdir()))`<br>`self.assertEqual(self.ops.original, self.ops.authorization)`<br>`self.assertRaises(JournalError)` | `self.assertEqual(64, len(result['runs']))`<br>`self.assertEqual(936, result['runs_unlisted'])`<br>`self.assertLessEqual(self.journal().path.stat().st_size, 16384)`<br>`self.assertEqual('account_done', result['phase'])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: StoreTests.test_the_successor_completes_its_adopted_record_after_recovery_and_reports_only_metadata

Replacement: `StoreTests.test_the_successor_completes_its_adopted_record_after_recovery_and_reports_only_metadata`. A1.11: new operation row shape adds two content-free durable integer counts. Closed key-set comparison remains exact and the actual counts (1,0) are pinned.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual({'predecessor_version', 'successor_version', 'state'}, set(report['records'][0]))` | `self.assertEqual({'predecessor_version', 'successor_version', 'state', 'runs_listed', 'runs_unlisted'}, set(report['records'][0]))`<br>`self.assertEqual((1, 0), (report['records'][0]['runs_listed'], report['records'][0]['runs_unlisted']))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: StoreTests.test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds

Replacement: `MemoryTakeoverTests.test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds`. MED-1/T-30: the former double-open fixture had no memory hold. A real SIGKILLed predecessor now leaves two memory holds, its inode is removed, and recovery must retain both. Every pre-existing cell except the diagnostic reason is compared, and two held rows are required. Premature release mutation is red.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual(before, self.rows('project_holds'))` | `self.assertEqual(2, len(before))`<br>`self.assertEqual([row[:-1] for row in before], [row[:-1] for row in after])`<br>`self.assertIn('instance_unproven', after[0][-1])`<br>`self.assertEqual(2, history._connection.execute("SELECT COUNT(*) FROM project_holds WHERE kind IN ('driver_entry','preparation') AND state='held'").fetchone()[0])`<br>`self.assertTrue(all((observed.get(name) == value for name, value in facts.items() if name != 'reason')))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: ProcessProofTests.test_process_scan_past_limit_is_unproven_and_never_signals

Replacement: `ProcessProofTests.test_linux_foreign_flock_holder_is_busy_without_descriptor_reads`. A1.4 removes the census and its 4096-process/5-second limits. T-36 statically forbids it, candidate-only native pinning passes, and Linux foreign-flock ownership is checked without reading descriptors. This supersedes, rather than weakens, the retired scan requirement.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertRaises(TakeoverError)`<br>`self.assertRaises(TakeoverError)` | `self.assertTrue(process.foreign_lock_holder(42, [path]))`<br>`self.assertFalse(process.foreign_lock_holder(99, [path]))` |

### packages/codex-agents/tests/test_b4t_takeover.py :: ProcessProofTests.test_process_arguments_and_fd_scan_past_byte_limits_are_unproven

Replacement: `ProcessProofTests.test_process_arguments_past_byte_limit_are_unproven`. A1.4 removes every descriptor read. The original oversized-argument OSError remains in the renamed test; T-36 forbids fd APIs and the real native candidate test passes.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertFalse(process.Kernel().sole_holder(os.getpid(), path))` | `self.assertRaises(OSError)` |

### packages/codex-agents/tests/test_b4t_takeover.py :: OwnerIntegrationTests.test_J6a_adds_a_late_settled_entry_but_not_an_older_tombstone_after_restart

Replacement: `OwnerIntegrationTests.test_J6a_excludes_both_old_and_late_settled_entries_after_restart`. A1.12 T-35 explicitly excludes settled entries, including first seen at J6a. Both restart and repeated reconciliation now require exactly the one unsettled run.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertEqual([run_entry(2), run_entry(3)], document['runs'])`<br>`self.assertEqual([run_entry(2), run_entry(3)], restarted.reconcile_runs(document, late=True)['runs'])` | `self.assertEqual([run_entry(2)], document['runs'])`<br>`self.assertEqual([run_entry(2)], restarted.reconcile_runs(document, late=True)['runs'])` |

### packages/codex-agents/tests/test_b4t_takeover.py :: MemoryTakeoverTests.setUp

Replacement: `JournalTests.setUp`. Equivalent closed comparison under the accepted additive target; existing original-cell checks are retained.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertTrue(select.select([child.stdout], [], [], 10)[0])`<br>`self.assertLess(child.returncode, 0)` |  |

### tests/test_e2e_update_safety.py :: RegistryTests.test_the_predecessor_is_an_unmodified_catalogue_listed_archive

Replacement: `RegistryTests.test_the_predecessor_is_an_unmodified_catalogue_listed_archive`. MED-5: vendored unmodified P66 archive, pinned SHA-256, runtime version assertion, no Git invocation in a shallow/offline fixture; corrupted artifact must fail with a digest reason.

| Old assertion(s) | New assertion(s) |
|---|---|
| `self.assertIn("'archive', PREDECESSOR_REVISION", SOURCE)` | `self.assertIn('tests/fixtures/b4t/0.8.66.tar.gz', SOURCE)`<br>`self.assertIn('hashlib.sha256(raw).hexdigest() != PREDECESSOR_ARCHIVE_SHA256', SOURCE)` |

### T-12b product-path replacement (no old assertion expression)

The old fake port ran under `os.kill`/`os.killpg` guards but never called product stop code. Its replacement is `CollectorStopTests.test_B4t_3_collector_and_new_services_are_never_signalled`: real `OwnerOperations.stop_collector` and `stop_service`, guards for kill/killpg/pidfd_send_signal, exact shutdown shape and exact `service.stop_for_update` call. Injecting a collector signal turns it red. No existing assertion was removed.

### MED-6: expected-value assignments, base → reviewed tree → final

| Existing test | Old oracle → replacement | Strength |
|---|---|---|
| `test_project_start.StartTests.test_E2c_7_another_version_s_live_host_is_not_replaced` | Base: `'recovery: needs update handover: ' + self.command('alpha')`; reviewed: `'recovery: needs update hand-over: ' + compatibility_message(...)`; final: literal `recovery: needs update hand-over: Another CoCoLo version (number unavailable) holds the project service. The local install record is unavailable. In a plain terminal, open your CoCoLo clone, run ./setup.sh with this project's path, then choose Configure, then "Update the install and walk through every step".` | Entire exact string, including punctuation and owner action, independent of the product builder. Live host, process identity, no-spawn and collector identity assertions all remain. |
| `test_project_start.OtherVersionTests.test_E2c_7_a_real_host_of_an_earlier_runtime_is_not_replaced` | Same three oracle stages and final literal as above. Fixture now creates its empty ledger using the earlier runtime's own owner open, before it starts; no published floor is lowered. | Entire exact literal; every live host and collector assertion is retained. This socket test still needs the primary rerun. |

### Previously omitted fixture/target changes against ce81bd96

| File / stimulus | Old → new | Preserved check / justification |
|---|---|---|
| `test_account_launch_claims.drop_admission_evidence_for_legacy_fixture` | Inline S2-epoch drop → `drop_runtime_authority_for_legacy_fixture`: remove floor/lineage/epoch before labelling a fixture pre-17; refuse a nonempty lineage. | No assertion removed. Historical DDL and source bytes remain historical; adopted custody cannot be relabelled. |
| `test_run_holds.schema_fourteen` | Drop register objects → first drop empty machine-local runtime authority, then the same register objects. | No assertion expression changed; all original-cell, hold and receipt comparisons remain. |
| `test_learning_source_image.test_schema_version_columns_and_primary_key_are_exact` | Newer unsupported stimulus `user_version=18` → `19`; supported 18 is exercised by existing supported-layout iteration and current source image cases. | The same `history_incompatible` refusal and unexpected-column/PK checks remain. |
| `test_learning_source_transfer.target_rows` | All project tables except epoch → all original tables except epoch and the four new machine-local authority tables. | Exactly the same tables/cells that existed at base are compared. New authority is not partition data; backup/restore validators separately exclude it. |
| `test_learning_source_image_legacy` | No direct diff against ce81bd96; shared downgrade helper now retires empty machine-local authority. | Every historical-layout and original-source assertion unchanged. |
| `test_backup_installed_project.test_*` unsupported-version loop | `(0,4,18)` → `(0,4,19)`. | Same `RuntimeError` at each unsupported boundary; exact supported schema 18 columns and table ordering are checked by the supported iteration. |
| `test_reply_shape_live.DRIVERS` | Existing producer list → existing list plus `test_b4t_takeover`. | No assertion removed; extra real producer coverage. New native socket tests are a separate module, so this addition does not introduce sockets into the B4t component module. |
| `test_e2e_update_safety.test_handover_keeps_every_original_row_except_the_explicit_writer_epoch` | Snapshot without schema → literal accepted schema-23 hash in both snapshots. | Same positive and tamper negatives, now additionally checked against a separately pinned schema. |

### HIGH-2 / L11.2 strictness

Base `history_kept(..., exact=True)` → `handover_history_kept(...)` now calls `history_kept(..., exact=True)` after allowing only the existing epoch row's value to differ and new keys in `takeover_record`, `takeover_runs`, `takeover_obligations`, `runtime_floor`. Existing authority rows remain exact; epoch key additions/deletions fail. The before schema must be pinned 22 or 23 and the after schema must be pinned 23. Added `outcome:2`, hold/result/unknown/epoch rows, changed original rows and unexpected DDL all fail the negative unit witness. Thus no business-row or schema comparison is weaker than ce81bd96.

### Renames retaining every assertion

The real kernel candidate-pin test retains its readiness, exit and lock-free comparisons under its A1 name. The formerly standalone SIGKILL/missing-inode memory test becomes T-30 with all its cell, reason and held-count assertions retained. The oversized argument test retains its OSError under its new name. Retired two-opener/census cases map to A1 E's real cross-process flock refusal and T-36's ban on all descriptor/process censuses.

## S5 final fix

The E2c-7 literal oracles change from the "local install record is unavailable" guidance to the bound local
command (`cd <root> && ./setup.sh <root>` between the same lead and closing words), because the fixture's
recorded launch copy carries an install binding and D2.8/B4m then require the command. The OtherVersion fixture's
ledger is now originated by the earlier runtime instead of being deleted after this build opened it. The native
fixtures pass `CollectorPaths` to hint readers. Three native tests move to their own modules unchanged. Every
old → new pair, with reasons, is in [assertion-audit.md](assertion-audit.md), "S5 final fix".

## S5 regression-only fix (gate exception)

Op-set pins: each change adds only an op the design adds. D2.9: "Op-set pins change only by the new read
operation(s) and `service.stop_for_update`". D2.8 names the reads (`history.takeover_report` and
`account.takeover_report`). H4b and A1.5 row 7 (`stop_op`) name `service.stop_for_update`. A1.10 bounds the
`account_takeover_report_client` page loop. No op was removed, and no op outside these three was added.

| Test | Old → new | Design citation |
|---|---|---|
| `test_account_host` `...reads_status_and_nothing_that_spends` | `OWNER_OPERATIONS` pin gains `account.takeover_report` | D2.8, D2.9: the owner terminal's `learning-status` reads it; read-only, spends nothing |
| `test_admission_inputs`, `test_signature_inputs`, `test_signature_job_intake` captured `ACCOUNT_OPERATIONS` | set gains `account.takeover_report` | D2.8, D2.9 |
| `test_b2_service_outcome` `AuthorityTests.test_operation_set_unchanged` | `HISTORY_OPERATIONS` pin gains `history.takeover_report`, `service.stop_for_update` | D2.8, D2.9, H4b, A1.5 row 7 |
| `test_holds_report_socket` `test_B7_1_2_3_6_learning_status_from_plain_terminal` (also run through `test_b7b_terminal`) | requests `['history.holds_report']` → `['history.holds_report', 'history.takeover_report']` | D2.8: learning-status adds hand-over lines from the one new project read |
| `test_updfix` `test_UPDFIX_2_refused_needs_both_a_child_and_a_replacement` | `refused == children and replace` → `refused == False` (the `children_running` and `would_replace` checks are unchanged) | D2.1/SW-3 ("B4t removes only UPDFIX's `child_run_active` refusal"), B4t-0. The product's `refused = unreadable` is from the earlier B4t rounds |
| `test_updfix` `test_UPDFIX_2_a_claimed_unreleased_run_is_a_child_that_may_be_running` | fixture: the test's own open ledger is closed before the check. The assertion `(would_replace, refused) == (False, False)` is unchanged | The test says "No collector runs here". Under A1.5 row 10, an open ledger holds `collector.lock` without answering `health`, so it is a `collector_unresponsive` collector. The fixture now builds the scenario its comment states |

Not changed, so the primary must decide: `test_candidate_evidence_lookup` AP3. The test needs released 0.8.61
(ledger schema 17) to open the current ledger. H1d ("Every binary before B4t refuses a store past its own
schema") and T-5 require schema 18 to refuse it. No released schema-18 build exists yet.

Product changes (no test edits): `learning_store._migrate` keeps a pre-18 ledger that already carries the exact
B4t authority tables, and upgrades it instead of refusing it (owner 129.3). Its floor still binds.
`cli.py learning-status` makes the account takeover read only when there is a project report for it to complete.
`client_runtime.account_takeover_report_client` ends its account-endpoint enrollment.
