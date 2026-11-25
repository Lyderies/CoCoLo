# Unit 3.8 slice 4 status

Base `230c2fc0` (VERSION 0.8.62), branch `feat/ap5-evidence-lookup`.
Implementation and requested requirement checks complete; changes left uncommitted
in the named worktree because this session cannot write Git metadata.

## Requirement evidence

The complete requirement → test → evidence map is in [ap5-lookup.md](../ap5-lookup.md).

- AP5-2: `CandidateEvidenceLookupTests.test_AP5_2_fixed_links_have_flat_vm_work_as_unrelated_records_grow`
  failed before code (10,076 → 210,063 steps), passes after code (164 → 148);
  the former scan grows 8,005 → 148,005. A preliminary implementation exposed
  SQLite choosing the type index for invalidation (4,651 → 94,623); explicit
  digest-index selection closes that second scan without changing the test.
- AP5-3: `test_AP5_3_mixed_evidence_answers_and_link_order_match_former_scan`
  and `test_AP5_3_public_admission_duplicates_and_history_order_match` pass
  before and after replacement. Expected refusals are asserted independently
  as well as compared to the former algorithm. Public append, duplicate replies,
  stored evidence order and history sequence/order match.
- AP3: `test_AP3_digest_index_is_additive_on_first_open` and
  `test_AP3_indexed_copy_opens_reads_and_appends_with_real_previous_release`
  failed before the index existed and pass afterward. The latter extracts the
  recorded previous release's real native package using `git archive` from
  source commit `782a7eed`, recorded as 0.8.61 in `previous-releases.json`.
  Isolated Python imports that source (checks its path, release and schema 17),
  opens a populated indexed copy, checks all original records/order/late values,
  validates candidate links, writes a late event, checks SQLite integrity, and
  reopens. Current code then reopens and verifies the copy and prior write.
  No release-code monkeypatch, schema downgrade, reauthorization or service used.

## Named runs

Use the prepared test interpreter as `$PY` from the repository root. Every runner
invocation below reported `metadata_errors: []`; the new module is registered as
scenario/integration and AP3/AP5-2/AP5-3 have reciprocal requirement-source links.
Times below are runner worker wall time, with preflight shown separately.

1. Red: `$PY -B scripts/parallel_tests.py --module codex:tests.test_candidate_evidence_lookup`:
   5 tests, 3 expected failures, 2 behavior checks pass; 2.6 s, preflight 6.90 s.
2. Final focused/source/copy run: modules `tests.test_candidate_evidence_lookup`,
   `tests.test_type_lookups`, `tests.test_append_lookup`,
   `tests.test_learning_source_image`, `tests.test_learning_source_image_legacy`,
   `tests.test_learning_source_transfer`, `tests.test_backup_installed_project`,
   `tests.test_backup_reply_private`, `tests.test_account_observation_backup`:
   **80/80 pass**, 5.5 s, preflight 6.90 s.
3. Additional copier/enum run: `tests.test_candidate_evidence_lookup`,
   `tests.test_learning_migration`, `tests.test_large_project_backups`,
   `tests.test_owner_backup_streamed`, `tests.test_refusal_steps`,
   `tests.test_reply_shape_compatibility`: **85/87 pass**, 22.3 s,
   preflight 6.96 s. The two migration CLI failures are described below.
4. Final existing-store/runtime run: `tests.test_learning_store` and
   `tests.test_runtime_release`: **97/103 pass**, 6.5 s, preflight 6.99 s.
   Store: 95/100 pass; five socket-bind errors. Runtime: 2/3 pass; the sole
   failure is the intentionally unchanged release digest catalogue.
5. Baseline classification: extract untouched `230c2fc0` Codex package/tests
   and scripts using `git archive` into a disposable temporary directory;
   run `$PY -B -m unittest tests.test_learning_migration` from that copy's
   `packages/codex-agents` with its scripts on `PYTHONPATH` and a short `TMPDIR`.
   **15/17 pass**, 17.22 s. The same two CLI failures reproduce at the same
   assertions: `MigrationCliTests.test_command_moves_reports_refuses_and_rolls_back`
   (project-store cycle falls back to the retired account ledger) and
   `MigrationCliTests.test_first_start_of_the_project_service_moves_the_history_by_itself`
   (`orchestration_client_unavailable`). These are baseline sandbox/service
   failures, not changes caused by this index.

For runs 2–4, prepend `codex:` and pass each listed module using its own
`--module` argument. No full suite, affected selection, live install, commit,
push or service-owner mutation was performed. No pre-existing assertion changed;
see [assertions.md](assertions.md).

## Limits and handoff

Unix socket binds are denied by this sandbox. Existing collector socket tests
cannot complete here; the CLI migration checks that require the project service
also fail. The older-release compatibility test uses a real subprocess but no
socket or PTY, and passes here. No PTY test is required by this slice.

Blocked existing `LearningStoreTests` methods:

- `test_collector_rejects_forged_binding_and_unavailability_is_bounded`
- `test_second_collector_cannot_replace_the_active_socket`
- `test_app_server_cli_normalizes_and_ingests_through_collector`
- `test_collector_serializes_concurrent_clients`
- `test_spoofed_project_manifest_cannot_replace_active_collector`

The store tracebacks report `PermissionError` at the Unix listener's `bind`,
wrapped as collector endpoint unavailable. The primary should rerun these five
checks and the two named migration CLI checks in an environment allowing sockets.

`test_runtime_release.test_current_runtime_is_recorded_for_the_current_version`
is expected to fail because the collector runtime changed. VERSION and
`runtime-releases.json` are deliberately untouched; the primary owns release
bookkeeping at merge. The index is additive only; record/table/column formats
are unchanged. Project-history and project-holds builders were not edited.
AP4 first-open timing and full-suite validation are outside this requested slice
and remain for the primary; no requested requirement check is unrun.

## Files changed

- `packages/codex-agents/native-package/.codex/orchestration/codex_agents/learning_store.py`
- `packages/codex-agents/tests/test_candidate_evidence_lookup.py` (new)
- `scripts/test-layers.json`
- `scripts/test-stages.json`
- `scripts/test-timings.json`
- `scripts/test-requirements.json`
- `tests/requirements/ap5-lookup.json` (new)
- `docs/requirements/ap5-lookup.md` (new)
- `docs/requirements/ap5-lookup/STATUS.md` (new)
- `docs/requirements/ap5-lookup/assertions.md` (new)
