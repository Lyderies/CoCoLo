# RS-F S6 status

Implemented on `feat/rsf-s6-classify`, base `230c2fc0` (VERSION 0.8.62).
The fix round starts at `4a00f22d`; its changes remain **uncommitted**, as required
for the sandbox. The original S6 implementation is in that starting commit.
No VERSION, runtime digest catalogue or permanent schema change. No install,
merge, push or live-service handover. B4t consumes S2's fence for live handover.

## Requirement → test → evidence

Tests below are methods of `tests.test_rsf_s6_classify.ClassificationTests`.
The final owner-fence/resumability rerun passes **29 tests in 32.3 s**,
including all seven S6 tests, all six S6 IDs, the real old executable fence and
all S3 resumability tests; `metadata_errors: []`, no timeout or budget failure.

| Requirement | Test | Evidence |
| --- | --- | --- |
| RSF-S6-1 | `test_mixed_store_backfills_orchestration_open_duty_and_preparation_without_guessing`; `test_each_class_commit_survives_sigkill_and_two_reopens_identically` | Open and paused sealed orchestrations stay held at their activity-based due times; a closed one gains no hold. Ten exact subprocess SIGKILL boundaries, before/after each of the five class commits, reopen twice to the uninterrupted store. Orchestration adoption already existed and passed that portion before S6; no artificial red is claimed. |
| RSF-S6-2 | Mixed-store test above; `test_missing_legacy_inputs_keep_context_and_custody_without_fabrication` | Red missing duty → green. Original journal/lease verification yields a claimed tombstone for the finished run and a held duty for an unresolved open. Missing withdrawal inputs retain `legacy_open_inputs_missing` and a plain next step. A successor withdrawal pass leaves that duty unchanged. Already recorded historical registers remain byte-identical (existing B2 preservation test). |
| RSF-S6-3 | `test_unrecoverable_running_attempt_waits_for_b4s_lost_and_refuses_late_publication` | Red missing classification → green. Unfinished reservation stays held with `legacy_attempt_needs_settlement`; arbitration installs `releasing`; the B4s outbox accepts only the verified closure and records `lost`. The ordinary orchestration report counts it abandoned, and settlement replay is known. The finished attempt arbitrates as recovered. |
| RSF-S6-4 | `test_finished_result_remains_recordable_only_by_current_fenced_runtime_after_two_reopens`; missing-input test above; `test_other_runtime_guidance_keeps_custody_and_names_setup_update` | Red other-runtime refusal of a verifiable finished result → green under the current fenced runtime after two reopens. An arbitrary runtime still refuses with unchanged rows. Result and pre-existing records stay byte-identical. A real old bind without stored inputs becomes `legacy_no_context` with held custody and a next step. Guidance now names the existing setup update command. Fresh-store runtime mismatches remain refused by unchanged B2 tests. |
| RSF-S6-5 | Mixed-store test; SIGKILL equivalence test | Seventy real old unarmed leases cross the 64-source-row batch boundary. They remain held with `legacy_preparation_memory_lost` and a plain next step. Driver/preparation memory is not reconstructed; existing report fields remain `not_tracked`, zero invented memory holds. |
| RSF-S6-6 | Lost/late-publication test above; SIGKILL equivalence test; `test_revoked_epoch_rolls_back_classification_and_its_cursor` | Red checkpoint absence and later red retirement side effect → green. Late publication before and after lost settlement, and after restart, changes zero database rows, total_changes, context files or body files. A real epoch change at reservation commit rolls back classification and cursor together. The first uncommitted checkpoint restores schema 18; later checkpoints retain committed progress. |

## Fixture and implementation

The common mixed fixture is built by an unmodified git archive of `f4e8cea8`
(the pre-B3 schema-18 runtime). Its historical test helpers use real account and
project operations for the finished result, sealed orchestration/checkpoints,
original host opens, leases, bind and publication. Enrollment and the running
attempt's claim identity are explicitly synthetic; no provider process is
launched. Its lost closure uses the product's real closure signer with the
disposable fixture key, as the B4s store tests do. Account-service transport is
covered by existing modules, not claimed by this local store test.

No source database row is hand-edited or schema-downgraded in the S6 fixture.
The missing-input test extends the same fixture using the archived bind method.
Runtime epoch changes are labelled fault injection against a disposable store.
The real-process tests kill only their exact child PIDs, never a name/pattern.

The existing five S3 source classes now classify all historical project
versions, including schemas 18–21. The source window, adopted rows, evidence and
cursor commit under the same S2 owner transaction; no extra unbounded adoption
pass exists. Schema 22's existing holds, contexts, custody, settlements and JSON
evidence suffice. Ordinary opens do not run adoption. Original before-copies,
pending-startup refusal, frozen classification time and final validation remain
S3's. Earlier orchestration-only pending markers can gain the missing result
classes with the original clock sample.

Finished recording permission is carried by custody evidence bound to the
original binding, context digest and result digest after verifying stored inputs,
consumed grant and result body. It requires the authority's runtime to equal the
current fenced owner's runtime. Per the 129.3 reading (LOW-2), permission follows
whichever runtime holds the fence at admission, including a later upgrade; it is
not pinned to the runtime that performed migration. Normal admission still checks the original open
orchestration, host class, association, terminal and retention; this grants no
publication or launch authority and never changes original runtime bindings.

Evidence-only waits retain their original obligations. Their durable evidence
names the reason and an existing command: inspect with `learning-status` or
`learning-run-status`, or start the project service with
`learning-start-project-service`. The service's existing withdrawal/claim and
B4s settlement paths consume verified evidence. Unarmed leases release on expiry
or successor cleanup; uncertain armed claims never release on a timer. Missing
original evidence remains an evidence wait. No closure is invented by migration.
Memory without any durable source remains explicitly untracked.

## Named verification and limitations

Structured, content-free outcomes are in [evidence.json](evidence.json), with
reproducible named commands in [commands.txt](commands.txt). No raw logs, machine
paths or owner identifiers are stored in these documents.

- Registered initial red: three tests, all fail, 1.8 s, `metadata_errors: []`.
  Added checkpoint/epoch coverage then ran red (five tests, 3.8 s).
- Core regression run: 252 tests in 44.9 s. Its sole failure was the existing B2
  already-recorded preservation assertion; production was corrected without
  changing that test. The next B2 run passes all 55 tests.
- Later core: 169 pass plus one expected runtime digest-pin failure, 52.7 s,
  `metadata_errors: []`. Runtime inventory and completeness checks pass.
- Copier/source-image/backup/restore/move/enumeration: **532 pass**, 35 named
  modules, 106.2 s, `metadata_errors: []`, no timeout or budget failure.
- Recording/orchestration/custody/S2 regressions: 305 tests, 63.4 s. Only the
  two S2 adoption-wrapper subcases failed after a callback keyword changed its
  function signature. Production now preserves the original two-argument API;
  no assertion or existing test was changed. The final named rerun passes the
  complete S2, S3 and S6 modules (29 tests, 32.3 s).
- Service-dependent copier checks were attempted with a short TMPDIR: 188 tests,
  17.6 s, four named modules. Six workers fail on Unix socket creation/service
  startup (`Operation not permitted`, unavailable collector/project endpoints).
  That original sandbox attempt was not green. The primary subsequently reran
  `tests.test_learning_migration`, `tests.test_learning_store`,
  `tests.test_state_location` and `tests.test_open_withdrawal` unsandboxed and
  reported all four green at `4a00f22d` (188 tests; owner-provided evidence).
  No timeout is presented as a pass. No PTY-specific test was required or run.

A mistyped module name stopped one regression preflight with no tests run; it
was corrected to `tests.test_holds_report` and the complete named batch rerun
with `metadata_errors: []`. Bootstrap fixture-envelope corrections occurred
before the registered behavioral red baseline. Neither is claimed as a product
red test. All existing assertions remain intact; corrections to the new test's
first-checkpoint assertion are paired in [assertions.md](assertions.md).

## Files changed

Production: `project_history.py`, `project_holds_schema.py`, `service_record.py`,
`refusal_steps.py`. Tests: `test_rsf_s6_classify.py`, `rsf_s6_fixture.py`. Metadata:
the layer, stage, timing and requirement registries plus `tests/requirements/rsf-s6.json`.
Documentation: `docs/requirements/rsf-s6.md` and this directory's status, assertion
audit, commands and structured evidence. The original S6 implementation modified
no pre-existing test file; this fix round strengthens the existing S6 test module.

Final source hygiene: `git diff --check` and the identifier scanner pass.
The fix-round verification and current owner-shell rerun are recorded below.

## Fix round

Scope: MED-1, MED-2 and LOW-1 through LOW-4 only. Production edits stay in
`project_history.py`'s S6 classifier and `refusal_steps.py`; no S6 restructuring,
new command, reason code, wire operation, table, column or record shape. VERSION
and the runtime digest catalogue are unchanged. RS-B's downstream worktree was
not touched.

All test names below belong to `tests.test_rsf_s6_classify.ClassificationTests`.

| Review item / requirement | Test or documentation | Evidence |
| --- | --- | --- |
| MED-1 / RSF-S6-2/3/4/5 | `test_each_new_owner_step_names_an_existing_cli_command`; `test_other_runtime_guidance_keeps_custody_and_names_setup_update` | Seven step literals failed the CLI inventory before code changes. Each now names a parser-registered command or `bash setup.sh --update`, the existing setup entry point; the setup test also verifies `--update` exists. A fresh-store mismatch uses that same refusal guidance, without a nonexistent recovery flow. Existing B2 fresh-store runtime refusals still pass. |
| MED-2 / RSF-S6-2 | `test_schema21_expired_lease_uses_retained_denial_receipt_to_clear_open_duty`; `test_schema21_adopted_denial_without_receipt_clears_from_sealed_no_launch_evidence` | Both `denied` and `fenced` receipt cases ran red with a held duty. Migration now uses the same `fenced` release as `settle_denied_result_lease`, preserving the original lease tombstone and receipt. A real pre-B1 schema-17 account denial, upgraded by archived schema-21 runtime `e933d3a6`, additionally proves B1's receipt-less adopted denial clears with no invented receipt. Its behavioral assertion also ran red. Two reopens preserve every row. A mid-batch clock advance additionally ran red before using S3's frozen migration time for the release. No owner confirmation is needed. |
| LOW-1 / RSF-S6-6 | `test_unrecoverable_running_attempt_waits_for_b4s_lost_and_refuses_late_publication` / `late_refused` | Both APIs assert exactly `history_result_expired` (message and reason code) while releasing, after lost settlement and after reopen; database rows, `total_changes` and context/body files remain unchanged. These strengthened assertions passed before the fix; no artificial red is claimed. |
| LOW-2 / RSF-S6-4 | Finished recording permission paragraph above | Explicitly records the 129.3 reading: permission follows the runtime that currently owns the fence, including later upgrades. |
| LOW-3 / RSF-S6-5 | `test_unarmed_lease_guidance_names_the_existing_automatic_expiry` | Red old guidance → text naming the existing unarmed expiry. The actual sweep releases all 70 unused leases as `withdrawn_unsent`. No indefinite-hold promise. |
| LOW-4 / RSF-S6-3 | `rsf-s6.md`, synced requirement source and registry | Requirement now explicitly maps lost to the report's `abandoned` count and the hold's `lost` release kind. The existing settlement test asserts both. |

The schema-21 fixtures use actual archived executables. The receipt-bearing
cases extend the schema-18 mixed fixture and use the product closure signer with
the disposable key (synthetic account closure, no transport claimed). The
receipt-less case uses `dd7e5074`'s real in-process authenticated account denial
and project closure, then the real 17 → 21 upgrade. Enrollment and account billing
attestations are labelled fixture controls. No project source row or schema is
hand-edited. A verified sealed expired lease plus its verified host open is
positive no-launch evidence under B1's existing adoption rule; a wall-clock
deadline, missing inputs or S2's predecessor exclusion alone is never enough.
Unverified/unresolved opens keep their original conservative assertions.

Fix-round verification (named modules only):

- Initial red: 10 tests, 3.8 s, 11 assertion failures; first green: 10 tests,
  4.0 s. Both have `metadata_errors: []` and no timeout/budget failure.
- Receipt-less supplemental behavioral red: 11 tests, 6.2 s,
  `metadata_errors: []`. A prior fixture-precondition failure is documented in
  `assertions.md` and is not counted as a behavioral red.
- First regression batch: 172 tests, 32.9 s, 166 pass; the six failures are all
  in `tests.test_project_hold_register`. Five cannot start the collector in this
  socket-restricted sandbox. The sixth,
  `RegisterAdoptionTests.test_the_step_adopts_every_lease_and_reservation_by_class`,
  expects adoption evidence `{18}` but S6 already writes `22` at `4a00f22d`.
  An archived-baseline named run reproduces exactly these six failures (30 tests,
  3.9 s). That existing assertion and production policy are unchanged in this
  fixed-scope round; primary must reconcile the pre-existing adoption pin.
- `tests.test_runtime_release`: 3 tests, 0.2 s, 2 pass and the expected
  `test_current_runtime_is_recorded_for_the_current_version` digest-pin failure.
  Inventory/completeness pass. The primary updates the release pin at merge.
- Broader batch after receipt-less correction: 173 tests, 34.8 s, 167 pass;
  exactly the same six independently reproduced register failures. All 11 S6
  tests pass (6.7 s). `metadata_errors: []`, no timeout/budget failure.
- Frozen-clock supplemental red: both receipt cases catch the fresh release
  clock instead of S3's frozen sample. The classifier now passes its migration
  time directly to the existing release primitive. Final named S6/S3 rerun:
  **21/21 pass, 32.4 s**, including all **11/11 S6 tests** (5.9 s), all six S6
  requirement IDs and S3 interruption/resumption coverage. `metadata_errors: []`,
  no timeout or budget failure.

All existing assertion changes in this round are paired in `assertions.md`:
bare `HistoryError` → exact expiry reason on both publication APIs; generic
`update` wording → real setup command and no recovery phrase. The supplemental
fixture correction is also audited. New test methods are registered under the
existing S6 scenario module; no new module or requirement ID was introduced.

Primary must rerun the recorded short-TMPDIR socket command on this final diff:
`tests.test_learning_migration`, `tests.test_learning_store`,
`tests.test_state_location`, `tests.test_open_withdrawal`, plus
`tests.test_project_hold_register` for its five service-dependent cases. The
last module also retains the independently reproduced baseline adoption-pin
failure described above. No PTY work is required. The prior 188-test unsandboxed
green belongs to `4a00f22d`, not to this fix diff.

Final `git diff --check` and identifier scan pass. No test process or full run
remains in flight. No commit was attempted under the sandbox restriction.

## Primary correction after the fix round
The fix round's broader run reported `test_project_hold_register` (`{18}` adopted evidence) as a baseline failure; it is green on
the 0.8.62 base, so it was an S6 regression: classification overwrote the original `adopted` step of rows adopted at step 18.
Classification now adds `classified=22` and keeps `adopted`; the finished-result check in service_record.py reads `classified`.
No test changed. Unsandboxed rerun: project_hold_register, rsf_s6_classify, rsf_s3_resumable, rsf_s2_fence, project_holds_schema,
b2_service_outcome, publication_replay, project_results all green; learning_migration, learning_store, state_location,
open_withdrawal green.
