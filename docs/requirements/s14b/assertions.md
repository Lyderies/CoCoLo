# Assertion changes

No assertions in tests present at the unit's base are removed or changed. S14 is absent from this unit's
base; its accepted measurements are used as a sourced projection input and its
backup boundary checks remain a primary integration gate when S14 is merged.

New S14b test revision, before outcome retention code: the real-footprint test's
`assertIsNotNone(unreferenced outcome)` is replaced by `assertIsNone(unreferenced
outcome)`, an exact outcome-count/status summary assertion and a digest lookup
returning that summary. Reason: the reader audit proves a 90-day learning window;
keeping all full outcomes failed measured sizing once released receipts and
partial summaries were included. Linked outcomes still have the original
preservation assertion, with an explicit evidence link in their fixture. This
strengthens the lifecycle requirement rather than weakening a storage threshold.

## Fix round (040-s14b-r1)

- All retention fixture writes previously called `_append_checked_locked` with
  unchecked/unlinked detail -> `store_product_row` validates each row, validates
  evidence, and invokes the product append (including `_auto_link_locked`) in
  one transaction. Outcomes first store a matching terminal host event. The
  integration outcome now belongs to the actual claimed run, rather than an
  unrelated synthetic run. This replaces both reviewed fixture bypasses.
- Unreferenced outcome sweep count `1` -> `2`, and summary `{'outcome': 1}` ->
  `{'event': 1, 'outcome': 1}`: the terminal host event and its linked outcome
  expire atomically. The released-capacity budget check also changes `1` -> `2`.
- Protected fixture total `3` -> `4`: product provenance adds the terminal host
  event, which remains protected together with the controlled outcome.
- `test_historical_lookup_past_SQL_work_limit_returns_plain_age_answer` expected
  an invented generic age answer after read refusal ->
  `test_historical_lookup_budget_failure_does_not_invent_membership` requires
  SQLReadError instead. A budget failure cannot establish that an ID existed.
  Indexed, typed member and typo checks at 365,001 summaries replace the previous
  unbounded JSON scan/fallback assertion. Existing not-found assertions remain.
- The integration live-run check additionally requires the outcome to remain
  present while its real binding/open is live. No footprint threshold is raised.

New checks cover a quiet protected tail remaining idle, the owner request's
existing 800 ms budget, a protection recheck inside the write transaction,
propagated completion replay fences, and rollback/release of a linked group.

- The former lookup mock's `assert_called_once()` is retained in the new refusal
  check. Its old outcome fixture/digest path is covered by the strengthened
  product outcome/status/digest test; the new refusal check uses a never-written
  ID to prove that exhausted work cannot invent historical membership.
- The footprint assertion now includes the derived lookup index, and executes
  after backup verification. The 16 GiB ceiling is unchanged: the corrected
  projection fails at 16.464 GiB. The earlier 14.219 GiB claim is superseded.
- New closed-contract assertions reject malformed member types. Existing
  lifetime-cap registry entries now name the indexed lookup check; the bounded
  linked-component entry names the oversized-group/no-starvation check.

## Final fix

- `test_real_open_claim_footprint_retires_to_measured_metadata_and_summary`:
  old `projected = retained/recent/event + 64 bytes × live membership count` ->
  planned backup projection excludes the TEMP index; the live database projection
  is reported separately with a 40-byte/member reserve. The existing strict
  `< 16 GiB` assertion remains, now bound directly to the unchanged product
  `learning_source_image.MAX_BACKUP_BYTES`; an additional `> .79 GiB` positive
  margin assertion and exact 12-byte key assertion strengthen it. This implements
  the explicit final product decision, rather than raising a storage threshold.
- `test_old_detail_resolves_its_own_summary_after_other_batches` and
  `test_typed_history_and_years_of_summaries_use_indexed_membership`: synchronous
  `rebuild_history_index()` completion -> schedule and advance bounded owner
  maintenance pages before the existing exact summary/type/not-found assertions.
  No lookup assertion is removed or changed. The years-scale fixture still uses
  validated product appends for all 365,001 summaries and 50,000 detail records.
- `test_summary_survives_original_source_image_and_learning_transfer`: existing
  summary count/digests/detail assertions remain; add byte-for-byte archive and
  byte-size equality, rebuilding response, exact original-owner/imported-owner
  lookup equality, and imported-owner restart/rebuild equality.
- `test_collector_sweep_sql_runs_on_the_E1b_owner_thread`: all original SQL thread
  and ordinary-work assertions remain; add a scheduled rebuild, safe lookup
  before maintenance, exact history after maintenance, and rebuild SQL thread checks.
- New regressions: 70-summary paged reopen/epoch-upgrade/restart, read refusal
  during rebuild (ID and content digest), compact WITHOUT ROWID index and stated
  collision bound, forced equal prefixes (including a lost same-type collision)
  without foreign data, byte-identical populated/empty-index backups and equal
  planned sizes, and restoration from actual backup summary rows.

Red evidence on e32ade83 before runtime changes: reopen test expected zero
startup-built members but got 70; forced-prefix seam and imported-history API
were absent; restored lookup returned immediate history instead of the required
scheduled rebuild response; product-path projection test rejected all 15
32-byte membership keys. Backup exclusion itself was already correct because
the old index was TEMP; its preserved byte-exclusion assertions passed before
and after this change. A fixture-only correction prepended `project_id` to
backup record columns (the manifest lists partition columns implicitly); it
reached the intended rebuilding assertion and failed before runtime edits.

- The years-scale test adds a strict `<= 40 bytes/member` compact-index reserve
  assertion; its original scale, typed-history and not-found assertions remain.
  Its old full-index build timing now measures all bounded maintenance pages.
- Added imported 71-summary paging, interrupted-page checkpoint, digest-answer
  equivalence and forced-candidate rejection checks, including automatic project
  cleanup. Added a second project to the ledger-rebuild regression.
- Added uppercase full-digest lookup equality: red first with not found during
  full-digest verification, then green after byte-normalizing the comparison.
  This preserves the former bytes-based lookup's case equivalence.
- No execution budget was increased: the final isolated 25-test module passes
  all functional checks but still reports 76.681 s against its 60 s runner budget.

- New `test_exclusive_vacuum_reschedules_rowid_based_history` fails first because
  the product deletion/VACUUM path did not schedule a rebuild; it now requires
  a reset checkpoint, a safe rebuilding answer and the same summary afterward.
  This protects the new cursor and numeric project references from VACUUM rowid
  changes. Fifteen original-store backup/migration/expiry assertions are unchanged
  and rerun green alongside this case and the new restore case.

## Regression fix (primary gate exception)

No base assertion was edited, removed or weakened. The four affected-run failures
(`test_ledger_lock_limits`, `test_account_observation_schema` x3,
`test_lifetime_cap_guard`) pass unchanged; the changes are in product code and in
tests that this branch added.

- `RunRetentionTests.setUp` and the five `RunRetentionIntegrationTests` setups
  (old -> new): the persistent retention indexes existed after any ordinary open;
  no open creates them (two base layout tests forbid it), so each fixture first plays
  the consented upgrade step (`run_retention.ensure_indexes(ledger)`, what
  `create_retention_indexes()` does). Every functional assertion is unchanged.
- `RunRetentionTests.test_typed_history_and_years_of_summaries_use_indexed_membership`
  moved unchanged in name and assertions to the new registered module
  `test_run_retention_scale.RunRetentionScaleTests` (budget). Differences: the
  first 2,000 of the 365,000 summaries still enter through the whole product
  append; the other 363,000 are each validated with `validate_learning_record`,
  serialised and digested by the product functions, indexed by the product
  `_summary_members`, and written in one batch with the same columns. The
  `<= 40 bytes/member`, old-scan-refused, typed/not-found and rebuild assertions
  are the same. `scripts/test-requirements.json` (S14b-4 check id),
  `test-layers.json`, `test-stages.json`, `test-timings.json` register the module.
- New cap registrations: `lifetime_caps.json` entries for
  `run_retention.py::advance_import_history` (2 checks, test
  `test_import_rebuild_pages_resume_and_preserve_digest_answers`, 71 imported
  summaries past the 32-row page) and `run_retention.py::rows` (1 check, test
  `test_reopen_upgrade_rebuild_is_paged_resumable_and_lookup_is_safe`, 70 summaries).
  The guard also flagged the nested `rows` page reader, not only the function the
  primary named.
- New `test_plain_open_makes_no_index_and_the_consented_step_enables_retention_with_a_silent_idle`
  (registered under S14b-3): a plain open has none of the five indexes and an idle
  retention pass runs no statement; after `create_retention_indexes()` there are five,
  a pass before the earliest expiry still runs no statement, and one at the cutoff
  sweeps.
