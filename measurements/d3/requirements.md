# D3 "Crash-safe retirement and the context sweep": requirement -> test -> code site

Base e8d2dfd2 (D2 head). Code: PRC = packages/codex-agents/native-package/.codex/orchestration/codex_agents/project_result_contexts.py,
PH = project_history.py in the same folder. Tests: packages/codex-agents/tests/test_retirement_sweep.py (35 tests, RS-D-D3).
Sources: reviews/040-rs-bf-a1/requirements.numbered.txt:115-118 (slice D3), DESIGN-0.4.0-RS-D.md draft 3 s4-s6 (:537-608),
draft 4 s1 (:832-), rows :696-700 and :957-960, ANALYSIS-0.4.0-rs-bf-audit.md (D3 rows).
Already done by D2 and kept: PRC.begin_retirement (custody released, context `retiring`), `lost_with_reservation` for a reserved row
whose obligation RS-B released, `_retire_due` / the `missing` hold policy, `_expiry_now`. No schema step and no new account operation:
`repair_required` and `repair_reason` were already in the step-18 table and its CHECK.

| Row | Requirement | Test (class.method) | Code site: before -> now |
|---|---|---|---|
| U1 | missing file: reads refused, charged, retired normally | UnsafeFileRetirementTests.test_U1_* | PH `_retire_result` skipped an absent name. Regression pin (green before) |
| U2 | corrupt (digest mismatch) | test_U2_* | read refuses `history_result_unavailable`; retirement never opened the file. Regression pin |
| U3 | wrong owner or mode | test_U3_* | PH `_retire_result` called `_result_file_info` before the unlink and raised, leaving the row `retiring` for ever. Now PRC.retire_names (323-352) lstat + unlink, never opens |
| U4 | extra hard link | test_U4_* (body, context, with an unrelated attempt in the same pass) | same; the other name, outside the store, is untouched |
| U5 | symlink, FIFO, link pair | test_U5_* | same; a symlink's target is never followed, a FIFO never blocks (no open). PH `_recover_result_link` is still called first but its refusal is swallowed (a clean pair loses its partial name; the unsafe rest is unlinked as entries) |
| U6 | directory under a fixed name: `repair_required:directory`, still charged, shown, retried; other rows progress. Unlink or fsync error | RepairRequiredTests.test_U6_* | PRC.refuse_retirement (354-364) marks the context `repair_required` + `repair_reason='directory'` (row stays `retiring`, charged); PRC.repairs (532-) and PH `result_retirement_repairs` (2429) show it with the directory names; PRC.begin_retirement (387) does not overwrite `repair_required`; PRC.end_retirement (406) / finish (367) clear the reason. Unlink or fsync error: row and context stay `retiring` (see Deviation) |
| U7 | context-only (unarmed, denied, fenced, lost unbound) retirement finished by the sweep, no reservation row | ContextSweepTests.test_U7_* | PRC.sweep (499-530) over PRC._sweep_candidates (480-497), called first in the PH `expire_results` side-pass loop (2514), own rowid cursor, own failure isolation |
| U8 | stop during context-only retirement (after `retiring`, after the unlink) | test_U8_* (three, incl. bounded and fair) | sweep selects the context's own `retiring` / `repair_required` retries; PRC.reconcile (445-) also finishes both at open |
| U9 | lease released `bound` with a reservation (reserved, committed, recorded): not selected | test_U9_* | `_sweep_candidates` NOT EXISTS reservation row |
| U10 | released `withdrawn_unsent`, `denied`, `fenced`, `lost_unbound`, plus `retiring` and `repair_required` retries: each finished, no reservation row written | test_U10_* | same; a trace of the connection shows no INSERT/UPDATE/DELETE on result_reservations |
| K1 | stop after `retiring`, body unlink, context unlink, directory fsync, before `expired`: next pass completes; charges kept until `expired`; no read in between | CrashSafeRetirementTests.test_K1_* (six) | PH `_retire_result` (2395-2428): `retiring` tx, retire_names, `expired` tx; reservation `retiring` is a selector candidate. Most stop points were already safe (green before); the `repair_required` restart one is new |
| K2 | a 1 MiB body read through every chunk; last chunk short; offset at the size refused | LargeBodyReadTests.test_K2_* | result_read_service.py handle (unchanged). Regression pin |
| S1 | start-up reconcile: orphans, partial files, link pairs, 64 live contexts; 0 body or context bytes read; work equal at 0 and 100,000 tombstones | StartupReconcileTests.test_S1_* | PRC.reconcile (445-478) + _remove_body_partials (420-443): directory listing, lstat and live-index rows only; a lone body partial and the partial name of a two-link pair go. The work is counted with sqlite's progress handler (480 VM steps at 0 and 490 at 100,000 tombstones) |
| SH-3 (retirement) / RS-D-6 | custody released `retention`, `orchestration_closed`, `lost_with_reservation` before the files go; `expired` only after | every test ends with `consistent()` (custody_consistent and register_consistent, SH-7) | project_holds_schema.py unchanged |

## Deviation (reported)
The design table (draft 3 s4) puts an unlink or fsync error in `repair_required:io`. D2's pinned tests (test_deadlines_retention
`start_retirement` / `assert_still_retiring`, test_project_results `test_failed_delete_keeps_capacity_and_retired_state_until_real_cleanup`)
assert that an unlink error leaves the row AND the context `retiring`, so an I/O error keeps `retiring` (charged, retried every pass, the
pass raises `history_unavailable`); only a directory, the unsafe file that needs an operator, is `repair_required`. Status wiring of
`result_retirement_repairs` into the owner-facing status is B7's projection (nothing reads it yet).

## Owner decisions
90.9 A (a box that would not stop: retire command) and 90.6 A (old-version records closed and shown): D3 touches neither (no stop path, no
migration). Not done here.

## Not in D3
S2/S3 (D4 replay, query plans), orphan body *final* names without a row (a body write always follows its reservation row, so none arises),
the `crashed` settlement (RS-C), status projection (B7).
