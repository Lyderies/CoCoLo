# P9 status — implemented; deferred OS execution still needs owner validation

Worktree: `cocolo-p9`, branch `fix/o0c-p9-attest`. No commit (sandboxed), push,
install, service change, VERSION bump or release digest catalogue edit. The new
helper is included in `runtime_files.py` so protected runtime copies include it.

Requirements were recorded in [../p9.md](../p9.md) before the public regression
and runtime edits. Durable runner summaries and per-test outcomes are in
[evidence.json](evidence.json). All completed named runs have
`metadata_errors: []`. No full suite was started; no test batch remains running.

| Requirement | Named check in `tests.test_signature_inputs` | Evidence |
| --- | --- | --- |
| P9-1 | `RetainedAttestationClaimTests.test_public_claim_with_two_attestation_limit_keeps_cited_tied_history` | Initial red: post-collector capture raised `signature_capture_limit` with limit two (37 tests, 4.53 s). Final public reference claim passes with real SSH verification and usable Codex credits; only the latest tied valid attestation waives them. The capture limit is enforced at reference preparation, and shared admission candidates are compared exactly. |
| P9-1 | `RetainedAttestationClaimTests.test_public_deferred_claim_uses_the_same_two_attestation_candidates` | Capture, retention and quota checks pass, then real helper execution/cleanup times out. `/usr/bin/sandbox-exec` independently fails with `sandbox_apply: Operation not permitted` (exit 71). Required deferred proof remains **unproved here**, not green or skipped. |
| P9-2 | `test_public_claim_with_two_attestation_limit_keeps_cited_tied_history` | Pass. Real collector `_sweep_loop`, deterministic clock/wait, twelve scheduled intervals, batch two: three uncited obsolete rows removed, no pass removes over two, cited records/signatures/links remain readable, latest valid survives invalid newer signature. No manual direct ledger sweep drives this scenario. Existing four-reference-family retention test also passes unchanged. |
| P9-3 | `SignatureInputOwnerTests.test_P9_projection_invalidates_mutations_and_rebuilds_after_restart` | Red on record-type mutation; green after exact type and owner-generation binding. Content, digest, source, sequence, observation time, both signatures and new owner generation invalidate results. Restart starts with no derived results; bounded maintenance rebuilds. Capture performs no verification or writes. |
| P9-3 | `SignatureInputOwnerTests.test_P9_failed_verification_is_retried_on_later_bounded_passes` | Red with empty rows after the verifier recovered; green after negative results were retried. |
| P9-4 | Public reference scenario plus existing row/byte/precopy tests | Pass. Two remaining account candidates fit exactly; row limit one and one byte below exact retained size refuse before body copy. Unknown/changed rows remain conservatively charged. All pre-existing assertions are unchanged. |

## Implementation and limits

`attestation_capture.py` owns a connection-local SQLite TEMP projection. Each
result binds exact record content/metadata, signature and full recorded owner
generation. Only verified later rows of the same project/provider/account allow
supersession; cited rows stay in durable history. Shared predicates feed
signature headers/bodies and admission headers/bodies, so exact recapture sees
the same candidates. Selected signatures still go through ordinary verification.

The sweep examines at most a page and deletes at most a page per cycle. Its
cursor advances past cited and invalid rows, wraps, and retries failed
verification. The existing default is 100 rows per 60-second full collector
interval. After restart or edits, captures can refuse until maintenance examines
the necessary history. TEMP results are never imported/backed up and vanish at
connection close; persistent ledger schema and record shapes are unchanged.

`learning_store.py` changes are local: initialize the TEMP projection, delegate
the existing sweep, and share the candidate predicate in the attestation read.
No store-owner fencing code was changed. The lifetime guard's old sweep entry
moved to the helper with all three per-pass SQL bounds and the public scenario.

## Named-module validation

- Final signature run: **39/40 pass**, 29.00 s, metadata errors empty. Only the
  real deferred-helper scenario fails for the OS restriction above. P9-2,
  P9-3 and P9-4 are passing in the runner; P9-1 remains failed because its
  required deferred check cannot finish here.
- Core batch: 129 tests, 29.79 s. `test_admission_inputs`,
  `test_signature_job_intake` and `test_learning_retention` pass. The new
  lifetime-bound registration initially failed red; its later rerun passes.
  `test_event_retention` has one socket-bind error (`Operation not permitted`).
- Compatibility batch: **95/95 pass**, 29.28 s: `test_lifetime_cap_guard`,
  `test_large_project_backups` (both shards), `test_backup_reply_private`,
  `test_account_observation_schema`, `test_account_cleanup_schema`,
  `test_account_open_schema`, `test_project_holds_schema`,
  `test_legacy_capacity_upgrade`.
- Copier/backup/move batch: 170 tests, 45.14 s. `test_owner_backup_streamed`
  (both shards), `test_backup_installed_project`,
  `test_account_observation_backup`, `test_learning_source_image`,
  `test_learning_source_image_legacy`, `test_learning_source_transfer`,
  `test_reply_shape_compatibility` and `test_refusal_steps` pass.
  `test_state_location` has collector-start/socket failures;
  `test_collector_read_scope` has denied socket binds and consequent waits;
  two CLI checks in `test_learning_migration` fail with
  `project learning has moved to its project store`. No baseline run establishes
  those migration failures as pre-existing; they remain an owner follow-up.
- `test_runtime_release`: file-set and nonempty-runtime checks pass; only the
  release digest assertion fails, as expected under the instruction to leave
  VERSION and the release digest catalogue unchanged.
- An earlier `test_account_claim_jobs` / `test_admission_history_growth` batch
  was interrupted with Ctrl-C after repeated sandbox worker failures. It is not
  a completed acceptance run. A compatibility preflight containing
  `test_reply_shape_live` refused because that module requires `--profile soak`;
  no tests ran in that invocation. That soak module remains unrun.
- `git diff --check` passes. No new test module was introduced; the existing
  signature module remains scenario/integration. P9-1 through P9-4 have reciprocal
  requirement metadata. Assertion development corrections are fully recorded in
  [assertions.md](assertions.md); no pre-existing assertion was changed.

## Remaining owner commands

Use the supplied test venv (`PY`) on a host allowing subprocess sandboxing and
Unix sockets, with a short writable TMPDIR. Named modules only:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_signature_inputs --module codex:tests.test_account_claim_jobs --module codex:tests.test_admission_history_growth
$PY -B scripts/parallel_tests.py --module codex:tests.test_event_retention --module codex:tests.test_collector_read_scope --module codex:tests.test_state_location --module codex:tests.test_learning_migration
$PY -B scripts/parallel_tests.py --profile soak --module codex:tests.test_reply_shape_live
```

The primary owns the merge-time release digest update and final release gate.
