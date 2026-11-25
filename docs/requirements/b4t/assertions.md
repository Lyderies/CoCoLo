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
