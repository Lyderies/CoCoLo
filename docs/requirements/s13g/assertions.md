# S13G assertions changed (decision 389)

No budget or debt ceiling was raised. Every change is old -> new with its reason.

## tests/test_run_retention_scale.py (S14b-4)
| Old | New | Reason |
|---|---|---|
| One build of 365,000 summaries (2,000 product appends + 363,000 batch rows) and 50,000 detail rows, measured once | Two sizes, 20,000 and 80,000 summaries (a year and four years at ~200 batches/day), 10,000 detail rows (same ~1:8 share); each size runs the same measurements | Cause: 20 of 45 s were SQLite `executemany` inserts into the records table and its indexes (365k rows), plus 730k member rows written twice (batch insert, then dropped and rebuilt by `rebuild_history_index`). Insert time grew with load (37 s alone in a 12-unit run, 57 s in the load run, 68-85 s in full runs). |
| `assertLessEqual(index_bytes/(N+1), 40)` once at 365,001 | same assertion at both sizes | unchanged property |
| Old JSON walk raises `SQLReadError` under the 25 ms read budget at 365,001 | same assertion at 80,001 only (the walk takes ~120 ms there, 4.7x the budget; at 20,000 it takes ~30 ms, near the budget, so it would prove nothing and flake) | the oracle needs a size clearly over the budget; 80,000 keeps a 4x margin |
| Three lookups (found, wrong type, typo) once | the same three lookups at both sizes | unchanged |
| (none) | NEW growth claim: bytes per entry at 80,000 <= 1.15 x at 20,000; rebuild pages at 80,000 within 3.5x..4.5x of the pages at 20,000 (linear in the history, not quadratic) | the scale claim now holds by growth ratio over a 4x step, as S14E-3 does, instead of by one large size |
| `bulk_summaries` also inserted `retention_members` rows | no member insert in the bulk helper | the following `rebuild_history_index` drops the table and rebuilds it from the stored summaries before anything reads it, so the batch rows were never observed (no assertion depended on them) |

## tests/test_project_start.py
| Old | New | Reason |
|---|---|---|
| `StarterBase.setUpClass` ran `compileall` of the runtime into a per-class cache (15 classes) | one cache per test process, compiled once (`shared_pycache`), removed at exit | 0.5 s alone each, several seconds under load; the S13C-1 property (compiled before any test clock starts) is kept |
| `StarterBase.CONSTANTS` `STARTER_CHECK_SECONDS` 0.2, `START_RECHECK_SECONDS` 1.0 | 0.1 and 0.5 | named test intervals; each wait for a start or a recheck is shorter, and every window below holds at least as many starter passes and rechecks as before |
| `time.sleep(1.5)` "rechecked, still not replaced" (E2c-7 twice) and "a serving recheck never replaces an active probation" | `time.sleep(1.0)` | 1.0 s now holds two rechecks (0.5 s) and ten claim passes (was 1.5 rechecks / 7.5 passes) |
All other assertions in both files are unchanged.
