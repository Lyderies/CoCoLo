# S2: changed-runtime closure

Sources: DESIGN-0.4.0-RS-D.md, RS-D-2 narrowing (draft 2 §3), draft 3 §9 and N1/N2;
ANALYSIS-0.4.0-rsd-rse-closure.md, slice S2 and RS-D table.

**RS-D-2-N1 — Eligible changed-runtime closure.**
After a runtime change, the real `end_earlier` sweep closes an eligible orchestration as
interrupted. Recorded results and verified account releases permit closure. The ordinary
expiry pass releases committed custody `orchestration_closed`. Count each update-ended
orchestration with any unrecorded result once as `ended_by_update_unrecorded`, including
after restart and result retirement/purge. Expose only a count, never result content.
Same-runtime and other-service sweeps do nothing. The close and its count commit atomically.

**RS-D-2-N2 — Unfinished changed-runtime custody.**
Without verified account release, an unrecorded result prevents close. Keep the orchestration
open and committed body/context custody charged until its retention deadline; at that deadline
release custody `retention`, without inventing release evidence or counting an update closure.
Corrupt release evidence cannot close it. No pre-commit deadline retires reserved custody (SH-4).

## Shared invariants and implementation

SH-3/4/7: preserve release reasons; every new store test ends with both validators via `consistent()`.

`end_earlier` keeps the ordinary `apply` close path and its eligibility checks. In that transaction,
it releases an orchestration register row `closed` (taking one for pre-B6 journals that have none),
with a boolean `ended_by_update_unrecorded` evidence field. The tombstone survives result/orchestration
purge. `ProjectHistory.runtime_close_status()` returns only the integer count for the later S1 status
projection; wiring the full learning-status report remains S1's scope.

Tests: `tests.test_rsd_runtime_close`. The service case runs the real account terminal
and release handlers and project release verification, with synthetic protected cleanup
custody written using disposable fixture keys (as in `test_account_cleanup_release`).
No route, service or socket path is patched. Provider/enrollment are fixtures; no model calls.

No existing assertions change. No release bookkeeping or schema change is needed.

## Red and green evidence

Before production edits, `python3 -B -m unittest -v tests.test_rsd_runtime_close` ran
3 tests in 1.468 s: all three errored at the missing `runtime_close_status` method.
The closure, verified-release eligibility and custody deadline assertions already passed
on the base; those behaviors are pinned as regressions, not claimed as newly implemented.

- N1: `RuntimeCloseTests.test_N1_recorded_closes_and_releases_custody_without_counting_unrecorded`
  and `RuntimeCloseServiceTests.test_N1_N2_real_release_then_update_counts_unrecorded_once_across_restart`.
  The latter also checks missing/corrupt acknowledgement, atomic rollback/retry, no double count,
  and count persistence after reopen and result purge. Its cleanup journal is a synthetic fixture;
  account terminal/release handlers and project proof verification execute unpatched.
- N2: `RuntimeCloseTests.test_N2_unrecorded_without_verified_release_stays_open_until_custody_deadline`.
  Open orchestration, unchanged charges/readable body before retention; custody `retention` at equality;
  update-unrecorded count stays zero.

Final affected runs (all with `-B`):
- `unittest tests.test_rsd_runtime_close tests.test_project_orchestration tests.test_run_provenance`:
  60 tests, 10.012 s, passed.
- `unittest tests.test_deadlines_retention tests.test_retirement_sweep tests.test_record_context`:
  113 tests, 17.400 s, passed.
- `scripts/parallel_tests.py --module codex:tests.test_rsd_runtime_close`:
  3 tests, module 1.9 s, passed; no metadata, requirement or budget failures.
- `scripts/generate_test_metadata.py --catalogue`: passed using the existing
  `/private/tmp/cocolo-testfw-venv` (system Python lacks Hypothesis).

Assertions changed in existing tests: none. Every new store case ends in `consistent()`.
