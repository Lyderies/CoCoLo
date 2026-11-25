# Batch 0.8.61 merge notes

Merge branch `merge/batch-0861` (base 0.8.60). Order: fix/budgets-238, fix/recovery-line, feat/rsb-b7b, fix/o0c-p9b (includes P9),
fix/o0c-m11-dispatch, feat/rsf-s2-fence, feat/t7-s6, feat/t7-s5. No behaviour of any unit was changed in the merge.
Schema ends at ledger 17 / project 22 (RS-F S2); P9's temporary tables are per-connection (no schema).

## Files changed by more than one unit

| File | Units | Result |
|---|---|---|
| project_holds_report.py `holds_report` | B7b, RS-F S2 | Real textual conflict. S2 made the owner check read-only (`_require_owner(read_only=True)`); B7b added `recovery_pending` to the report. Kept both: read-only owner check, then the B7b report key. `recovery_pending` only reads, so it is safe under the read-only check. |
| result_persistence.py, project_holds_schema.py | recovery-line, B7b | Different functions; merged by git without conflict. Both behaviours present (RECOV-* and B7b tests). |
| learning_store.py, runtime_files.py | P9b, RS-F S2 | Different hunks; merged by git without conflict (S2 ledger 17 fence, P9 attestation maintenance). |

## Registries and shared test files

| File | Resolution |
|---|---|
| scripts/test-requirements.json | Conflict (RECOV-* vs P9-*). Union of both sides' entries, written with the same JSON format (indent 1, sorted keys). |
| scripts/e2e_core.py `GROUP_SECONDS` | Union: S6 groups `core_api`, `core_records` and S5 group `upd_all`. |
| scripts/e2e_lifecycle.py | Union: S6 and S5 check modules imported; `FAULT_MODULES` lists both slices' modules. |
| docs/requirements/t7/assertions.md | Both slices' sections kept (S6 then S5). |
| tests/test_e2e_requirements.py | S5 and S6 carry the same `deferred`/`reason` acceptance; one copy kept (git merged it cleanly). |
| other registries (test-layers, test-stages, test-timings, lifetime_caps, tests/requirements, tests/e2e/requirements.json) | Merged by git; all valid JSON; no duplicate e2e requirement ids. |
