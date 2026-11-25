# RS-F S6 status

Implemented in the named worktree on `feat/rsf-s6-classify`, base `230c2fc0`
(0.8.62 candidate). Changes remain **uncommitted**, as requested for the sandbox.
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
| RSF-S6-4 | `test_finished_result_remains_recordable_only_by_current_fenced_runtime_after_two_reopens`; missing-input test above; `test_other_runtime_guidance_keeps_custody_and_names_update_recovery` | Red other-runtime refusal of a verifiable finished result → green under the current fenced runtime after two reopens. An arbitrary runtime still refuses with unchanged rows. Result and pre-existing records stay byte-identical. A real old bind without stored inputs becomes `legacy_no_context` with held custody and a next step. Guidance separately ran red before replacing the instruction to start the old runtime. Fresh-store runtime mismatches remain refused by unchanged B2 tests. |
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
current fenced owner's runtime. Normal admission still checks the original open
orchestration, host class, association, terminal and retention; this grants no
publication or launch authority and never changes original runtime bindings.

Evidence-only waits retain their original obligations. Their durable evidence
names the reason and the owner's next step: check original account/open facts,
recover missing verified inputs or bytes, then use authenticated withdrawal or
B4s settlement during drained update recovery. No closure is invented by
migration. Memory without any durable source remains explicitly untracked.

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
  These checks are **not green**; the owner-shell command is recorded. No timeout
  is presented as a pass. No PTY-specific test was required or run.

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
audit, commands and structured evidence. No existing test file was modified.

Final source hygiene: `git diff --check` and the identifier scanner pass.
No test process or full run remains in flight. The four socket-dependent named
modules above still require the recorded owner-shell rerun.
