# S13 status: DONE with two product findings (RS-B-9 B6 mechanism, RS-10 production drain)

Follow-up: [S13a](../s13a/STATUS.md) resolves finding 1 and flips its pinned assertion.
The figures below remain the accepted S13 baseline; finding 2 remains for S13b.

Branch `feat/s13-budgets` on 0.8.66 (afb6f610). Test-only: `packages/codex-agents/tests/test_s13_budgets.py`,
`packages/codex-agents/scripts/measure_s13.py`, registry entries, docs. Requirement map: [../s13.md](../s13.md).
Raw numbers (macOS, CPython 3.14, disposable stores, 100,000 run): [measurements.json](measurements.json).
No product file changed, no VERSION bump, no existing assertion changed (no assertions.md).

## Fix round (review 040-s13-r1, NOT-READY, five findings)

| Finding | What changed | Result |
|---|---|---|
| HIGH B6: live-lease skipping stood in for RS-B section 5 | Measures the pass's selection size per batch (rows fetched at once from the lease/hold tables, through an instrumented connection) and the blocked rows given a back-off. The observable budget and the mechanism are separate requirements (RS-B-9-B6, RS-B-9-B6-MECH). | **Mechanism NOT met**: 179 rows selected in the one batch (128 blocked + 50 arrivals + 1), 0 rows backed off; same 3,067 VM steps at 0 and at 100,000 tombstones. Observable budget met (released in 1 batch, 0.7 ms). |
| HIGH RS-10: direct `limit=64` calls, no scheduling | `measure_due_holds_production`: the driver's real `_expire_results` (10 s cadence) drives the real writer lane's `expire` pass, whose lease pass is `limit=10`; virtual clock, real pass durations added. | **Budget NOT met**: 100 passes of 10, 990 s (60 released by 60 s); one pass about 4.5 ms. The direct 64-batch figure stays as RS-10-DUE (16 batches, max 16-18 ms, 0.23 s). |
| HIGH B1: only register call bodies | Times the whole `_release_obligation` (the reservation/hold JOIN), every register call, adds the commit cost the register rows add as a paired difference (9 pairs, same transaction without and with the writes), and the memory transactions. | Met: 1.2 to 1.9 ms of 5 ms across runs (register+JOIN 0.12 to 0.24 ms, commit delta 0.06 to 0.11 ms, memory writer 1.0 to 1.5 ms). The run's own commits (1.5 to 6.7 ms, baseline included) are recorded but not charged: they are the existing commits. |
| MED: releases per batch not asserted, no regression controls | `batch_released` recorded per batch; every check asserts at most 64. Controls (product patched in-test): 128 per call turns the due check red; a 6 ms slower JOIN turns B1 red; a pass that drains everything clears the production finding (the measure reacts). | Red/green shown by `S13RegressionControls` (4 tests). |
| MED: single wall-clock sample | Repeat protocol: fresh stores x3, median per batch position; B1 one warm-up then median of five; counts and VM steps decide, wall-clock is the loose bound (documented in s13.md and the test header). | A single pause moves one sample, not the median. |

Regression control note: the lease start pass has no back-off, so "remove back-off" cannot be patched out of the product.
That control is on the check (`b6_mechanism_violations`): rows shaped like a bounded, backed-off pass are clean; removing either part turns it red.

## Findings that need a new plan step (the product is not changed here)

1. **RS-B-9 B6 mechanism (RS-B draft 4 section 5).** `ProjectHistory.release_earlier_unarmed_leases` selects every
   `reserved` lease by rowid on every call, skips live ones, and never writes `next_check_ms` or a back-off. Work per batch
   grows with the number of blocked rows and nothing orders them by check time. Proposed step: bounded start pass for leases
   (select at most 64 due rows from `project_holds_due` ordered by `next_check_ms`, back off blocked rows 1 s doubling to 5 min,
   reset on release), with the B6 mechanism test flipped to `[] == violations`.
2. **RS-10 production drain.** The service releases at most 10 unarmed leases per 10 s pass (`result_persistence.py`
   `limit=10`, driver `EXPIRY_SECONDS`), so 1,000 due holds take about 990 s against the 60 s budget. RS-B draft 3 section 12
   says a full batch queues the next on the next driver step; that is not implemented for the lease pass. Proposed step: run
   the lease pass at the batch of 64 and queue the next pass on the next driver step while a pass was full (plus a driver-level
   test of the cadence), then flip RS-10-DUE-PROD to `[] == violations`.

Both tests that name these findings (`test_b6_finding_...`, `test_rs10_finding_...`) pin the measured miss, so they go red
the moment either step lands; replace the pinned assertion with the real one then.

## Measured against budget

| Row | Measured | Budget |
|---|---|---|
| B1 added writer work of a normal run | 1.2 ms (0.12 register incl. JOIN, 0.06 commit delta, 1.04 memory writer) in the 100,000 run; 1.9 ms in the last suite run | under 5 ms |
| B6 observable: one eligible behind 128 blocked, 50 arrivals before each batch | released in 1 batch (1 row), 0.7 ms (0 tombstones), 0.8 ms (100,000) | within 4 batches, at most 64 each, each under 50 ms |
| B6 mechanism (selection 64, back-off) | 179 selected, 0 backed off | NOT MET (finding 1) |
| RS-10 flat work at 100,000 released lease+reservation rows | 3,067 VM steps at 0 and at 100,000 | equal (tolerance 16) |
| RS-10 1,000 due holds, direct batches of 64 | 16 batches, 64 each (last 40), max batch 16 ms, total 0.23 s | at most 18 batches, at most 64 each, under 50 ms, 60 s |
| RS-10 1,000 due holds, production cadence | 100 passes of 10, 991 s | 60 s: NOT MET (finding 2) |
| RS-D S3 six budget tests (`root:test_rsd_budget`) | rerun: 6 tests pass | per `docs/requirements/rsd-s3/` |

## Verification (unsandboxed, TMPDIR short dir, --jobs 4)

`scripts/parallel_tests.py --jobs 4 --module codex:tests.test_s13_budgets --module codex:tests.test_b7b_budgets --module
codex:tests.test_holds_report --module root:test_rsd_budget`: `metadata_errors []`, `failed_units []`,
`requirement_failures []`; 39 codex + 6 root tests; test_s13_budgets 6.8 s (10 tests), test_b7b_budgets 31.8 s. Timing in
`scripts/test-timings.json` raised to 7.0 s.
Rerun the 100,000-tombstone numbers: from `packages/codex-agents`, run `scripts/measure_s13.py` with `tests` importable
(PYTHONPATH=. and `import tests.support` first), same python (about 25 s).
