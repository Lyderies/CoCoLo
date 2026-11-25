# P9 R1 fixed-scope corrections

Base: `5579560c`, worktree `cocolo-p9`. MED1, MED2 and LOW1 are implemented.
No commit (sandboxed), install, push, service change, VERSION bump or runtime
release-catalogue change. No full suite was started. The owner review records
MED3 as already resolved by its unsandboxed signature-module run.

Requirements were updated before the regression tests and implementation.
Original-code red results are preserved in [r1-red.log](r1-red.log); two further
exact-input/subprocess regressions have their own red logs. Durable named-run
summaries, per-worker failures, requirement outcomes and metadata results are
in [evidence.json](evidence.json), under `r1_fix_round`.

| Item / requirement | Test in `tests.test_signature_inputs.SignatureInputOwnerTests` | Evidence |
| --- | --- | --- |
| MED1 / P9-4 | `test_P9_capture_vm_work_is_independent_of_cited_superseded_history` | Original code refuses the 10,000-row capture with `signature_capture_work_limit`. Green: the two public claim-input capture boundaries select the same real latest attestation, retain all 10,001 records, and replay all four production SELECTs with progress interval 1. Zero/10,000 cited superseded rows give exactly **[56, 37, 105, 66] / [56, 37, 105, 66]** VM steps for signature headers/body and admission headers/body. The allowed difference is 100 per query. |
| MED2 / P9-3 | `test_P9_invalid_verdicts_are_cached_and_verification_holds_no_lock` | Red: 100 `(True, True)` lock/transaction observations. Green: 100 `(False, False)`, then three additional bounded calls including a full wrapped pass leave the call count at 100. Invalid rows remain excluded. |
| MED2 / P9-3 | `test_P9_failed_verification_is_retried_on_later_bounded_passes`, `test_P9_verifier_race_and_exceptions_leave_inputs_unchecked` | Red: TimeoutExpired/RuntimeError escape; actual subprocess timeout was separately converted to a cached False ([r1-timeout-red.log](r1-timeout-red.log)). Green: subprocess timeout, RuntimeError and OSError leave rows charged; real recovered verification succeeds. Signature, generation and record-type changes during verification publish no stale verdict. |
| MED1 + MED2 / P9-3 | `test_P9_projection_invalidates_mutations_and_rebuilds_after_restart` | Green: all original exact-field, owner-generation and restart assertions remain. Additional direct signature-deletion assertion went red ([r1-invalidation-red.log](r1-invalidation-red.log)), then green. Unknown/changed inputs become charged atomically. |
| LOW1 / P9-2 | `test_P9_due_cursor_advances_past_cited_rows_with_bounded_work` | Red: cursor `[0, 0, 0]`. Green: `[11, 21, 31]`, each batch-10 pass under 20,000 VM steps; a later uncited row beyond the cited prefix is eventually deleted while the latest survives. |
| P9-1, P9-2, P9-4 | Existing public reference claim, retention and hard quota tests | Green: real synchronous public claim, latest verified applicable evidence, cited history/signatures, all four reference families, unchecked foreign-provider charging, byte/row refusal before copying, and exact recapture assertions remain. Deferred OS helper execution still requires the owner's permissive host. |

The ten targeted regressions and budget checks pass; measured output is in
[r1-targeted.log](r1-targeted.log). Assertion and fixture replacements are
fully enumerated in [assertions.md](assertions.md). Every assertion replacement is documented; no accepted safety or quota
guarantee was relaxed. In particular, the pre-existing work/deadline tests now use
unchecked attestation candidates instead of requiring an unrelated event scan.

## Implementation

`attestation_capture.py` maintains TEMP exact checks with indexed current and
superseded flags, plus indexed candidate membership for each public provider.
Unchecked rows have membership for both providers. Capture headers and bodies,
and admission headers and bodies, start from that index; they never traverse
retained superseded attestations. TEMP writer triggers preserve exact-input
invalidation and dependencies when a checked row/signature/generation changes.
A reverted edit can reuse its exact cached check. These mutation paths can
refresh the derived projection; capture itself does no writes or cryptography.
The projection is connection-local, absent from backups, and rebuilt after restart.
Persistent ledger schema, record shapes and public operations are unchanged.

The sweep snapshots at most `batch` rows under `_held`, verifies and parses
outside both lock and transaction, then rechecks generation, signer and every
stored input before publishing. Both definitive valid and invalid results are
cached. `OwnerGrantAuthority` has an opt-in infrastructure-exception propagation
mode used only by this sweep, so its existing callers retain their behavior.
The deletion cursor uses the indexed flags to read at most `batch` candidates
before testing citations, and advances even when every candidate is cited.

## Named-module validation

All completed runners have `metadata_errors: []`; no new module or requirement
ID was added. The existing signature module remains scenario/integration.
The evidence file retains the initial runner failures as well as later results.

- Compatibility batch: **205/205 pass**, 24.37 s. Modules: large project backups,
  backup reply privacy, account observation/cleanup/open schemas, project holds
  schema, legacy capacity upgrade, streamed owner backup, installed project
  backup, account observation backup, both learning-source images, source
  transfer, reply shape compatibility and refusal steps.
- Copier/move/enumeration batch: **357 pass, 8 fail, 9 error, 3 skipped**, 136.40 s.
  All four merge-library modules and both learning chunk/QA upgrades pass.
  Learning-store failures are denied collector socket binds. State-location
  checks cannot start the collector. Collector-read-scope has denied binds and
  two helper/lock wait failures. Learning migration has two CLI failures
  (`orchestration_client_unavailable` and `project learning has moved to its
  project store`); the latter is **not established as environmental or baseline**
  and remains an owner follow-up. Runtime release has only its expected digest
  mismatch; file-set and nonempty-runtime assertions pass.
- Core final batch before the subprocess-boundary strengthening: **124 pass,
  7 error, 1 failed**, 40.15 s. Admission inputs (26), signature job intake (19),
  learning retention (15), lifetime cap guard (4), and 43/44 signature tests
  pass. The one signature failure is the existing deferred helper timeout;
  seven launch-record errors are explicit denied collector socket binds.
- Last verifier/project-start batch, with short `TMPDIR=/private/tmp/p9-r1`:
  **119 pass, 31 fail, 10 error**, 44.83 s; metadata errors empty. All five owner
  key contract checks pass. Learning engine is 63/70: the seven failures/errors
  are operational QA sandbox execution/preflight. Signature inputs are **43/44**;
  all R1 checks pass, and only the same deferred-helper test times out (including
  cleanup). Project-start is 6/38: collector startup/socket execution prevents
  the other 32 tests from proving their outcomes; its released-ledger backup,
  restore and move test passes. Runtime digest mismatch remains expected.
- Final targeted checks: **10/10 pass**, 15.73 s, including the real subprocess
  timeout/retry and direct signature-deletion mutation. Across five named-module
  runners: 963 test executions including repeats, 278.48 s wall time in total;
  every individual batch is below five minutes. No full suite was run, no test
  remains in flight, and no sockets/PTY-dependent result is represented as green.

The primary should rerun failed named modules on a host permitting Unix sockets
and sandboxed helpers, using the supplied venv and a short writable TMPDIR:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_signature_inputs --module codex:tests.test_e2a_launch_record
$PY -B scripts/parallel_tests.py --module codex:tests.test_learning_engine --module codex:tests.test_project_start
$PY -B scripts/parallel_tests.py --module codex:tests.test_state_location --module codex:tests.test_collector_read_scope --module codex:tests.test_learning_migration --module codex:tests.test_learning_store
```

`git diff --check` passes. The primary owns the merge-time runtime digest update.
