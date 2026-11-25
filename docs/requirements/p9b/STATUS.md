# P9b review fix round status

Base `9333772e`, branch `fix/o0c-p9b`. The fixed scope is HIGH-1 and LOW-1
from review `040-p9b-r1`. Both are implemented and locally green. Changes are
left in the sandboxed worktree; no commit, VERSION bump or runtime digest
catalogue update. Earlier-round results remain in [evidence.json](evidence.json);
its `review_fix_round` section records this round separately without raw logs
or machine paths.

Requirements were clarified in `docs/requirements/p9b.md` and synchronized to
the requirement source and registry before tests. Five tests were added to the
existing scenario/integration module `tests.test_p9b_attestation_followups`;
no new module or requirement ID was introduced. All seven pre-existing tests
and assertions are unchanged. The new EXPLAIN assertion correction and counting
fixture correction are recorded in [assertions.md](assertions.md).

| Requirement / review item | Test in `P9bAttestationTests` | Evidence |
| --- | --- | --- |
| P9b-1 / HIGH-1 | `test_previous_owner_key_signature_is_cached_invalid_without_reverification` | Red: a real signature from the previous owner key stays unchecked after rotation. Green: real ssh-keygen produces a cached `(valid,current)=(0,1)` after one call; the next maintenance pass makes no verifier call and capture excludes the row. |
| P9b-1 / HIGH-1 | `test_truncated_signature_is_cached_invalid_without_reverification` | Red: truncated real signature stays unchecked. Green: cached invalid after one real verification and no verification on the next pass. |
| P9b-1 / HIGH-1 | `test_deterministic_malformed_signature_diagnostics_are_definitive` | Red: all six specified parser diagnostics raise. Green: exit 255 with each diagnostic is definitive invalid, including absent rejection stdout. |
| P9b-1 / HIGH-1 | `test_infrastructure_and_ambiguous_results_raise_and_leave_rows_unchecked` | Pass before and after: all seven specified infrastructure patterns override both rejection stdout and malformed-signature stderr. Five other exit/signal outcomes, three ambiguous stdout cases and a timeout raise in maintenance authority, remain unchecked/capturable, and recover without restart. |
| P9b-1 / regression guard | `test_unsafe_verifier_leaves_genuine_rows_unchecked_until_repaired`; `test_failed_verifier_process_is_retryable_without_restart`; `test_clean_signature_invalid_is_cached` | All unchanged and passing. Six unsafe executable/parent metadata cases remain uncached and recover with real verification; the existing mismatch stays cached. |
| P9b-2 / LOW-1 | `test_admission_union_uses_indexes_and_bounds_rows_before_final_sort` | Red: 2,001 union rows enter the final sort with limit 3. Green: indexed candidate and record-type SEARCH access, no full records scan, at most 6 inputs to the final sort, and the same globally ordered three-row prefix. The fixture interleaves 1,000 rows from each union branch and 1,000 unrelated events. |
| P9b-2 / regression guards | `test_candidate_count_and_open_rebuild_ignore_non_attestation_history`; `test_candidate_membership_tracks_record_type_changes`; `test_admission_keeps_other_evidence_outside_candidate_table` | All unchanged and passing. |
| P9b-3 / unchanged scope | `test_deleting_newest_restores_predecessor_for_every_record_delete_path` | Unchanged and passing for all four deletion paths. No P9b-3 production changes. |

The admission query now takes the ordered first `:limit` rows of each branch
before the final union sort and record/signature joins. The global first
`:limit` must be among those two bounded prefixes, so ordering and the overflow
sentinel are preserved. This bounds the final materialization to twice the
capture limit. Scope filtering and each branch's own ordering still use the
existing indexes and SQL work/deadline budget; this is not a claim of constant
work over all live eligible evidence. No persistent schema, record shape,
reason code or operation changed, so no copier/backup/move/enumeration expansion
is required.

Completed named-module runs, with the supplied test interpreter:

- Corrected red run, before production edits: P9b **12 tests**, with one failure
  and eight errors (six parser subtest errors plus two absent-cache errors),
  **2.201 s** unittest time / **2.429 s** worker phase. The error outcomes are
  the expected missing definitive verdicts, not fixture failures. The EXPLAIN
  and measurement corrections preceded this red run.
- Green run: P9b **12/12**, **2.207 s** unittest time; admission inputs
  **26/26**, **1.864 s** unittest time. Runner worker phase **2.425 s**, plus
  **6.929 s** metadata preflight. **38/38 passed**, `metadata_errors: []`,
  `budget_failures: []`, no partial run. No runtime edits occurred during runs.
- `git diff --check` passes. No test process remains in flight.

No full run was started. The primary's requested unsandboxed reruns of
`signature_inputs`, `admission_history_growth`, `billing_gate` and the P9b module
remain outstanding; the first three were not rerun here because their public
helper/socket paths had unresolved sandbox limitations in the previous round.
No socket/PTY-dependent outcome is claimed from this round. Runtime-release
checks were not rerun; the unchanged release digest catalogue is expected to
reject modified runtime files until the primary updates it at merge.

Primary command, with the supplied interpreter as `$PY` and a short writable
TMPDIR on the permissive host:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_signature_inputs --module codex:tests.test_admission_history_growth --module codex:tests.test_billing_gate --module codex:tests.test_p9b_attestation_followups
```

Files changed this round: runtime `learning_engine.py` and `account_service.py`;
`packages/codex-agents/tests/test_p9b_attestation_followups.py`;
`scripts/test-requirements.json`; `tests/requirements/p9b.json`;
`docs/requirements/p9b.md`; and this unit's `STATUS.md`, `assertions.md` and
`evidence.json`. Only HIGH-1 / LOW-1 and their tests, metadata and evidence changed.
