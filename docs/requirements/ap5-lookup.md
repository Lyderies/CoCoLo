# Unit 3.8 slice 4: account evidence lookup

Base: `230c2fc0` (0.8.62 candidate). Authority: PLAN-0.4.0 AP block and
ANALYSIS-0.4.0-p38-inventory slice 4, rows AP3, AP5-2 and AP5-3.

- **AP5-2:** Candidate evidence validation seeks the linked content identities by
  index instead of fetching every project record. A fixed evidence set must have
  flat SQLite VM work as unrelated events and outcomes grow; the former scan is
  the growing negative control. Include the candidate invalidation lookup.
- **AP5-3:** Preserve admission answers, refusal precedence, linked-evidence order
  and record history order on mixed fixtures, including missing and other-project
  evidence, mismatched champions, owner feedback and duplicate admission.
- **AP3:** Add only an index, with no schema version or owner action. An indexed
  account-ledger copy must open, read, validate and append with the recorded
  same-schema previous release's actual code in a separate process, then reopen
  correctly with this version. Retain the existing append/type index checks.

The focused lookup and compatibility checks live in
[`CandidateEvidenceLookupTests`](../../packages/codex-agents/tests/test_candidate_evidence_lookup.py).

## Fix round requirements

- MED-1 (AP3): measure first open, reopen, and the digest index DDL separately on
  a disposable synthetic ledger of roughly 300 MiB and at least 200,000 records,
  populated through public `append`, without accessing an owner ledger. Record
  database size and index size growth; explicitly report a build over about 1 s.
  The separate named module `tests.test_candidate_index_build` owns this measurement.
- MED-2 (AP3/AP5-3): drop the digest index and reopen through `LearningLedger`;
  both candidate lookups must retain their answers, including owner invalidation.
  On an already-open connection without the index, both lookups must raise a
  plain `LedgerError` with reopen guidance, never raw SQLite diagnostics.
- LOW (AP3): previous-release compatibility must use actual previous Codex code.
  If that source is unavailable in a shallow checkout, fail explicitly naming
  the recorded release and missing source commit; never skip the check. The
  checked-in Claude bundle does not contain the previous Codex runtime.

| Requirement | Test method | Evidence |
| --- | --- | --- |
| AP5-2 | `test_AP5_2_fixed_links_have_flat_vm_work_as_unrelated_records_grow` | Red: 10,076 → 210,063 VM steps. Green: 164 → 148. Former scan negative control: 8,005 → 148,005. Fixed three links; 1,000 → 21,000 unrelated alternating event/outcome rows. |
| AP5-3 | `test_AP5_3_mixed_evidence_answers_and_link_order_match_former_scan` | Baseline and replacement pass the same expected answer matrix; preserves input link order, refusal precedence and history. Includes foreign/missing evidence, mismatched task/champion and owner feedback. |
| AP5-3 | `test_AP5_3_public_admission_duplicates_and_history_order_match` | Baseline and replacement accept an actual verified 40-outcome cohort identically, return identical duplicate answers, and expose identical mixed record history and source sequence order in paired projects. |
| AP3 | `test_AP3_digest_index_is_additive_on_first_open` | Red before implementation; green after. Drop only the new index, reopen, compare every other schema object and `user_version`. |
| AP3 | `test_AP3_indexed_copy_opens_reads_and_appends_with_real_previous_release` | Red for absent index; green with real recorded 0.8.61 source (`782a7eed`). Isolated subprocess reads all retained records/order/late values, validates candidate evidence, appends, checks integrity and reopens; current code then reads the same copy correctly. |
| AP3 | `CandidateIndexBuildTests.test_AP3_large_synthetic_first_open_index_cost` | Fix round MED-1: 230,000 public appends; 286.461 MiB compacted pre-index ledger. First open 0.205403 s, index build 0.198591 s, reopen 0.005369 s; index adds 24.984 MiB. |
| AP3, AP5-3 | `test_AP3_reopen_recreates_digest_index_and_preserves_lookup_answers` | Fix round MED-2: drop, normal reopen, both queries preserve accepted and owner-invalidated answers against the former validator. |
| AP5-3 | `test_AP5_3_missing_digest_index_gives_plain_ledger_error` | Fix round MED-2: both queries red with raw SQLite errors → green with exact plain ledger error/reopen guidance. |
| AP3 | `test_AP3_missing_previous_source_has_actionable_failure` | Fix round LOW: missing source, timeout and missing Git red with raw exceptions → green with explicit failure naming release and source commit; never skipped. |

The growth fixture uses synthetic bulk storage rows with unrelated identities;
it measures the complete candidate evidence-link validator, including feedback
invalidation, rather than the whole append admission pipeline. It does not claim
to bound candidate eligibility/lineage or a growing feedback history (other AP
slices). The public admission test uses the existing real cohort/QA fixture.
The former validator oracle retains the pre-slice candidate and feedback rules.

Implementation: additive non-unique `records_project_digest(project_id,
content_digest)` plus indexed `IN` seeks for candidate links and their invalidated
outcomes. Explicit index selection prevents the outcome type predicate choosing
a scan of every outcome. No table, column, stored-record shape or schema version
change. Existing AP append/type/grant tests remain unchanged.

See [STATUS.md](ap5-lookup/STATUS.md) for named runs and sandbox limits and
[assertions.md](ap5-lookup/assertions.md) for assertion accounting. Account ledger
only; VERSION, runtime catalogue and project-store builders are outside this slice.
