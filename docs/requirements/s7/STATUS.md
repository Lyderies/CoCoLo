# S7 status (done; ready for review)

Base `ce81bd96` (0.8.67), branch `feat/s7-e2e3`. VERSION and the runtime digest catalogue unchanged. **No product file
changed**: every new test was green on the base. Requirement map: `docs/requirements/s7.md`. No existing assertion was
changed or removed (assertions.md: none).

## New files

- `packages/codex-agents/tests/test_s7_owner_load.py` (M4b-E2, M4b-E3): four load tests on the real `LedgerOwner`,
  `CollectorServer` dispatch and `AccountService`; 0.4 s.
- `packages/codex-agents/tests/test_collector_first_response.py` (S1-U9): the real collector host, two tests; 3 s.
  Measured here: first plain health 11 to 15 ms and first project health 10 to 17 ms after the sockets exist (bound 1 s).
- `scripts/e2e_checks_owner_load.py`, flow L16 in `tests/e2e/requirements.json` (L16.1-L16.3); faults `ingest_lost`,
  `health_stalls`; `tests/test_e2e_owner_load.py` (judges and strictness guards).
- Registered: `scripts/test-layers.json`, `scripts/test-stages.json`, `scripts/test-requirements.json`,
  `tests/requirements/s7.json`, `scripts/e2e_lifecycle.py` (import, fault module), `scripts/e2e_core.py` (group seconds).

## Red evidence (tests were written first and are green on the base, so each is shown red by a temporary product mutation, reverted)

| Mutation (product, temporary) | Red |
| --- | --- |
| queue serves one project until its backlog is empty (`service_queue.py` rotation) | `test_a_project_backlog_of_real_ingests_yields_turns_to_another_project` |
| a quarter of acknowledged ingests are not written (`learning_store.py`) | hooks-and-account test, overflow test, fairness test |
| queue refusals not counted (`ledger_owner_queue.py`) | overflow test |
| a second thread runs ledger work inline (`ledger_owner.py`) | all four load tests |
| plain health calls the owner (`learning_store.py`) | health test |
| first health delayed 1.5 s in the dispatch / owner loop start delayed 1.5 s (`learning_store.py`, `collector_host.py`) | both S1-U9 tests / the project one (`ledger_busy`) |
| lifecycle fault `ingest_lost` | L16.1 and L16.2 red, L16.3 green |
| lifecycle fault `health_stalls` | L16.3 red (also L16.1 and L16.2: the hook path asks project health, so a stalled health loses events) |

A `fifo_turns` lifecycle fault left L16.2 green: real processes never queue deeply enough to show it. L16.2 therefore
guards starvation only; the fairness proof is the in-process test (see s7.md).

## Found while writing the tests (not product defects)

- `records.source_sequence` counts per project; the owner's write order across projects is `rowid`. An order check on
  `source_sequence` would have passed a first-in first-out queue.
- Record ids carry the normalization time: two normalizations of one hook event differ. Tests match by turn id.

## Green runs

- `scripts/parallel_tests.py --jobs 4 --module` for `test_s7_owner_load`, `test_collector_first_response`,
  `test_e2e_owner_load`, `test_ledger_owner_wiring`, `test_collector_exit`, `test_e2e_requirements`: 90 tests, no failed
  unit, `metadata_errors: []`, `budget_failures: []`, 37 s wall.
- `scripts/e2e_lifecycle.py --execute` for the three L16 checks: all pass, 69 s (two projects set up, then the load).

## Open

Nothing for the primary to decide. The primary runs `--affected ce81bd96` before merge (the new e2e flow is only in the
lifecycle job, not in the unit stage).
