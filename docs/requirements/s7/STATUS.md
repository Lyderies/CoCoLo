# S7 status (done; ready for review)

Base `ce81bd96` (0.8.67), branch `feat/s7-e2e3`. VERSION and the runtime digest catalogue unchanged. **No product file
changed**: every new test was green on the base. Requirement map: `docs/requirements/s7.md`. No existing assertion was
changed or removed in the base; the fix round's changes to S7's own tests are listed in assertions.md.

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

## Fix round (review 040-s7-r1, NOT-READY, 5 MED; tests only, no product file changed)

Each fix was shown red by a temporary mutation (product mutations reverted, `git status` clean of product files).

1. Fair turns (`test_s7_owner_load.py`, fairness test): the order must alternate `A B A B A B` while both projects have
   queued work, then only A's remaining backlog (`order[:6]` and `order[6:]` compared exactly). Mutation: the queue keeps
   serving a project at the head while it has two or fewer items left (`service_queue.py`, order `A B B B A A A A`): the
   old assertions stayed green, the new one is red.
2. First account response (`test_collector_first_response.py`): new `test_S1_U9_first_account_answer_comes_within_one_second_of_the_sockets_being_up`
   sends `account.project` over the real account endpoint (owner enrolment, fresh proof per attempt) and stops the clock
   at the decoded answer; bound 1 s. Mutation: a 1.5 s sleep at the start of `AccountService._handle`: the new test is
   red (1.518 s), both health tests stay green. Requirement text, registry and `s7.md` mention the account endpoint.
3. Held phases (`test_s7_owner_load.py`): the owner queue is built with its injectable clock (`RequestClock`, frozen by
   `pause_owner`), and while frozen a caller's wait for its answer is an event wait on `handled` (the real
   `Event.wait(remaining)` inside `LedgerOwnerQueue.wait` is otherwise a real 800 ms bound). Mutation: a 1.5 s sleep in
   `wait_admitted` while the owner is held: the old file went red (health test: queued ingests expired), the new file is green.
4. Health judge (`e2e_checks_owner_load.py`): at least 3 answered replies each for plain and project health (a reply with
   `ok` and `ledger_busy` is an answer), no refusal of any kind for either (`response_invalid` and the like). Mutation
   (judge inputs): 40 quick `response_invalid` and 0 answers, and 28 answered + 2 `response_invalid`: the old judge returned `[]`,
   the new returns `project_health_barely_answered` and `project_health_refused`.
5. Account persistence: the ledger rows recorded during the load (project id, observed_at_ms, state; new rows by request key
   against a before snapshot) must equal the expected (project, request id) observations the callers were told, once each, all
   `committed`; missing or non-integer observation times are refused (`observation_timestamp_missing`). The ledger stores
   the request id as a keyed digest, so identity is matched by (project, observation time) per expected (project, request
   id). Mutation (judge inputs): six right-count rows attributed to the wrong project, and a missing timestamp: old `[]`, new
   `account_record_misattributed` / `observation_timestamp_missing` (pending rows: `account_record_not_committed`).

Limit: items 4 and 5 are proven by judge inputs (the runner's `ingest_lost` and `health_stalls` faults still drive L16.1
and L16.3 red); no new lifecycle fault was added.

Runs (unsandboxed, short TMPDIR): `scripts/parallel_tests.py --jobs 4` for `test_s7_owner_load`,
`test_collector_first_response`, `test_e2e_owner_load`, `test_e2e_requirements`: 21 tests, `failed_units: []`,
`metadata_errors: []`, `budget_failures: []`. `scripts/e2e_lifecycle.py --execute <the three l16 checks>`: all pass, 66 s;
with `--fault ingest_lost` L16.1 and L16.2 are red, with `--fault health_stalls` L16.1-L16.3 are red, as before. (A
`--flows L16` run is not the right command: it also runs the every-flow checks and took over 15 min under machine load.)
Changed assertions: see `assertions.md`.
