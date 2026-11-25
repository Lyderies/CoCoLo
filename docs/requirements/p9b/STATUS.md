# P9b final targeted allowlist fix

Base `1e6b5e13`, branch `fix/o0c-p9b`; confirming review `040-p9b-r2c`.
P9b-1 is implemented and locally green. Changes remain uncommitted in the
sandboxed worktree. VERSION and the runtime digest catalogue are unchanged.
P9b-2 and P9b-3 production code is unchanged in this round.

Requirements were updated before tests, then the tests were run red against
the original denylist before the final runtime edit. The existing scenario /
integration module and timing registration remain valid; three new checks
were registered under existing requirement P9b-1. No new module or requirement
ID, module-import I/O, schema or record-shape change was introduced.

| Requirement | Test in `P9bAttestationTests` | Evidence |
| --- | --- | --- |
| P9b-1 | `test_unallowlisted_stderr_is_not_cached_and_recovers_without_restart` | Red: all 14 cases cached a row. Green: memory allocation, libcrypto, EMFILE/EIO allowed-signers, EIO signature-file and unknown diagnostics, each alone and following an allowed line, leave rows unchecked/capturable and recover on real verification without restart. |
| P9b-1 | `test_rejection_requires_exact_stdout_and_only_allowlisted_stderr_lines` | Red: 34 cases failed to raise. Green: missing/extra/whitespace stdout, non-exact diagnostic prefixes/case/suffixes and blank lines raise. |
| P9b-1 | `test_deterministic_malformed_signature_diagnostics_are_definitive` | Green: all eight exact observed diagnostic lines with exact rejection stdout, no terminator/LF/CRLF, and combined allowed lines return definitive invalid. Replaces the former synthetic-fragment / absent-stdout assertion; see assertions.md. |
| P9b-1 | `test_previous_owner_key_signature_is_cached_invalid_without_reverification`; `test_truncated_signature_is_cached_invalid_without_reverification`; `test_wrong_namespace_signature_is_cached_invalid_without_reverification`; `test_clean_signature_invalid_is_cached` | Real ssh-keygen caches invalid once for wrong key, truncation, wrong namespace and payload mismatch; the next maintenance pass performs no re-verification. |
| P9b-1 | `test_infrastructure_and_ambiguous_results_raise_and_leave_rows_unchecked`; `test_failed_verifier_process_is_retryable_without_restart`; `test_unsafe_verifier_leaves_genuine_rows_unchecked_until_repaired` | All original assertions unchanged and passing, including infrastructure precedence and retry after repair. |
| P9b-2 | `test_admission_union_uses_indexes_and_bounds_rows_before_final_sort`; `test_candidate_count_and_open_rebuild_ignore_non_attestation_history`; `test_candidate_membership_tracks_record_type_changes`; `test_admission_keeps_other_evidence_outside_candidate_table` | All unchanged and passing. |
| P9b-3 | `test_deleting_newest_restores_predecessor_for_every_record_delete_path` | Unchanged and passing for all four deletion paths. |

The runtime now requires exit 255, exact rejection stdout (optional LF/CRLF),
and empty stderr or only exact members of an eight-line allowlist. Unknown
stderr always prevents a cached negative verdict. The eighth line is the real
`Signature verification failed: incorrect signature`, needed by the existing
payload-mismatch regression. Local OpenSSH 10.5p1 observations, including exact
stderr and empty/directory allowed-signers outputs, are retained in
[evidence.json](evidence.json), under `final_targeted_allowlist_fix`.
The empty/directory allowed-signers behavior and existing directory-constructor
check are documented in the requirement; no new signer-path check was added.

Assertion replacement and fixture correction are documented in
[assertions.md](assertions.md): synthetic diagnostic fragments with absent
stdout -> exact observed diagnostics with exact rejection stdout, plus explicit
absent-stdout rejection assertions. The retryability fixture now excludes the
setUp account; all its cache/recovery assertions are unchanged.

Completed named-module runs using the supplied interpreter:

- Initial red: 15 tests, 48 subtest assertion failures in two tests; 3.545 s
  unittest / 3.798 s worker phase; metadata errors empty.
- First green attempt: two failures (existing payload mismatch needed its exact
  real diagnostic; new retry fixture reused a cached account). Neither cache
  assertion was relaxed. This attempt is retained in evidence.json.
- Corrected red against original runtime: 15 tests, 48 assertion failures in
  the same two regression tests; 3.422 s unittest / 3.736 s worker phase;
  `metadata_errors: []`. All 13 other tests passed; no fixture errors.
- Final green: `tests.test_p9b_attestation_followups` **15/15**, 3.238 s worker;
  `tests.test_owner_key_contracts` **5/5**, 0.107 s worker. **20/20 passed**;
  worker phase 3.247 s, discovery 3.071 s, preflight 7.894 s;
  `metadata_errors: []`, `budget_failures: []`, `partial: false`.
- `git diff --check` passes; changed unit docs have no absolute home paths.
  No test run remains in flight.

No full run was started. No sockets/PTY are required by this narrow change;
no socket/PTY-dependent checks were attempted or blocked in this round. Earlier
socket-dependent follow-ups are not claimed resolved. Runtime-release checks
were not run: the unchanged runtime digest catalogue is expected to reject
modified runtime files until the primary updates it at merge. No copier,
backup, move or enumeration expansion is needed because no persistent schema,
record shape, reason code or operation changed.

Reproduce the final green run with the supplied interpreter as `$PY`:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_p9b_attestation_followups --module codex:tests.test_owner_key_contracts
```

Files changed this round: runtime `learning_engine.py`;
`packages/codex-agents/tests/test_p9b_attestation_followups.py`;
`docs/requirements/p9b.md`; `tests/requirements/p9b.json`;
`scripts/test-requirements.json`; and this unit's `STATUS.md`, `assertions.md`,
and `evidence.json`. No other production files changed.
