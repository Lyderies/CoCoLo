# P9b status

Base `38911cd4`, branch `fix/o0c-p9b`. P9b-1..3 implemented; no commit because
this worktree is sandboxed. VERSION and runtime digest catalogue are unchanged.
Requirements preceded tests, and the corrected regression module ran red before
production edits. No existing assertion was removed or changed; fixture-only
corrections are recorded in [assertions.md](assertions.md).

All new tests are in `tests.test_p9b_attestation_followups.P9bAttestationTests`.
The module is registered as scenario/integration, with timing metadata and
requirement sources in `tests/requirements/p9b.json`. Completed named runners
report `metadata_errors: []`. [evidence.json](evidence.json) preserves summarized
results and individual outcomes, without machine paths or raw logs.

| Requirement | Tests | Red → green evidence |
| --- | --- | --- |
| P9b-1 / D1 | `test_unsafe_verifier_leaves_genuine_rows_unchecked_until_repaired`; `test_failed_verifier_process_is_retryable_without_restart`; `test_clean_signature_invalid_is_cached` | Six unsafe executable/parent metadata cases and five failed-process cases cached negative verdicts on the accepted code. All now stay unchecked and capturable, then acquire a valid exact check after repair on the same connection. Cases include executable/parent ownership, writable modes and symlinks, signal death, OOM and ambiguous nonzero diagnostics. A real cryptographic mismatch remains definitively invalid and is verified once, then cached. |
| P9b-2 / D2 | `test_candidate_count_and_open_rebuild_ignore_non_attestation_history`; `test_candidate_membership_tracks_record_type_changes`; `test_admission_keeps_other_evidence_outside_candidate_table` | Red: 20,002 candidates for one attestation plus 10,000 unrelated records; events and provider observations entered membership. Green: two entries before insertion, after insertion and after reopen. Actual per-open TEMP initialization costs 1,335 / 1,336 SQLite VM steps with zero / 10,000 unrelated records. Checked and unchecked type transitions remove/restore membership. Public admission retains identity, billing and headroom outside this table. |
| P9b-3 / D3 | `test_deleting_newest_restores_predecessor_for_every_record_delete_path` | Red: deletion without a foreign-key signature cascade left the retained predecessor hidden. Green: direct SQL with and without cascades, public `expire`, and public `expire_report` restore the surviving verified predecessor immediately, without crypto or restart. Separate account groups survive independently. |

The corrected original-code run was **1 passed, 6 failed tests** (16 subtest
failures), zero errors, **2.9 s**. The final new module is **7/7 passed**.

The existing P9 scale and cursor assertions remain unchanged. Instrumented
named checks passed **3/3 in 21.322 s**: candidate rebuild, retained-history VM
work and bounded cited-row deletion. Both zero and 10,000 retained superseded
attestations require exactly **[56, 37, 178, 132]** VM steps for signature
headers, signature bodies, admission headers and admission bodies respectively.

The maintenance authority now raises on unsafe verifier prechecks and on
non-definitive process outcomes. Definitive negative classification requires
exit 255, the exact clean rejection stdout and either the exact cryptographic
mismatch diagnostic or the known complete missing-header parser rejection.
The latter is a definitive malformed signature, already exercised by the P9
public claim fixture; arbitrary parser/internal/resource failures remain
unchecked. Ordinary authority callers still return False on infrastructure
failures. Only maintenance collects verifier diagnostics.

Candidate initialization and writer triggers now include billing attestations
only. Admission unions these candidates with its five other indexed evidence
families under the existing scope. A TEMP check-deletion trigger repairs only
the removed current valid winner's indexed project/provider/account group.
Deleting already superseded rows does not rebuild all groups. No persistent
schema, record shape, reason code or operation changed, so the schema-change
copier/backup/move/enumeration expansion rule does not apply.

Completed checks:

- Core named run: **87.98 s**. New regressions 7/7, admission inputs 26/26,
  learning retention 15/15 and owner-key contracts 5/5 pass. Initial public P9
  claim errors exposed the missing-header negative classification gap, fixed
  without changing its assertions. The concurrent admission-history result was
  invalidated by an in-flight production edit and is superseded by a stable
  rerun below.
- Compatibility named run: **72.93 s**. Providers **106/106**, admission metadata
  **13/13** pass. Learning engine **63/70**: the remaining one failure and six
  errors explicitly fail operational sandbox preflight. Runtime release **2/3**:
  only the expected unchanged release-digest assertion fails. Billing-gate
  fixture digests were invalidated by the same in-flight edit; superseded below.
- Final stable signature/new-module run: **49.52 s**. New module **7/7**;
  signature inputs **43/44**, including the real synchronous public P9 claim
  and all scale, cache, quota, invalidation and retention checks. The remaining
  deferred public claim times out waiting for its helper and again during
  cleanup, matching the accepted P9 sandbox limitation; its admission outcome
  is unproven here.
- Stable admission-history / billing-gate rerun: interrupted at **190.05 s**
  after repeated failures, before either module completed, to keep this named
  run below five minutes. No complete outcomes are claimed. The runner's owned
  worker cleanup finished; its metadata records `runner interrupted:
  KeyboardInterrupt`. The cause of every failure in this incomplete run is
  not established. Both modules require an owner rerun; the initial digest
  errors are not presented as their stable result.
- Three direct, named admission-history scope checks pass on stable source,
  **3/3 in 1.860 s**: first passing Claude decision at any age, latest older
  Claude billing/headroom, and refusal when recapture precedes its anchor.
  These preserve their existing assertions and do not claim whole-module
  success. Existing fixture file-handle ResourceWarnings were non-failing.

No full suite was started. No socket/PTY-dependent outcome is represented as
passing without an observed pass. The owner should rerun deferred/helper and
operational sandbox failures on a permissive host; no permission escalation,
installation, service changes or runtime catalogue update was attempted.
`git diff --check` passes. No test run remains in flight.

Owner rerun, with the supplied test interpreter as `$PY`, a short writable
TMPDIR and a host permitting collector sockets and sandboxed helpers:

```sh
COCOLO_WORKER_TIMEOUT=180 $PY -B scripts/parallel_tests.py --module codex:tests.test_admission_history_growth --module codex:tests.test_billing_gate
COCOLO_WORKER_TIMEOUT=180 $PY -B scripts/parallel_tests.py --module codex:tests.test_signature_inputs --module codex:tests.test_learning_engine
```

Files changed: runtime `account_service.py`, `attestation_capture.py`,
`learning_engine.py`; new `test_p9b_attestation_followups.py`;
`scripts/test-layers.json`, `test-stages.json`, `test-timings.json`,
`test-requirements.json`; `tests/requirements/p9b.json`;
`docs/requirements/p9b.md` and this unit's `STATUS.md`, `assertions.md`,
`evidence.json`. Runtime files are under the Codex package's native orchestration
directory; the new test is under its tests directory.
