# RS-F S7 validation

Executed from `packages/codex-agents` using only named-module commands below.
Every completed run was under 300 seconds. Times are unittest's elapsed seconds.
No git commands or socket operations ran. Existing assertions were not edited.

## Acceptance → tests

| IDs | Module / coverage |
| --- | --- |
| RSF-S7-1 | `tests.test_rsf_s7_account.BindingReplayTests`: original withdrawal after schema15→16, actual record expiry, binding pruning and repeated reopening; changed supplied generation/digest; real keyed open-receipt ownership; different caller/attempt/inputs; legacy generation omission. |
| RSF-S7-2 | `tests.test_rsf_s7_account.SettlementReplayTests`: real sealed armed lease and keyed claim receipt; settlement release once; schema15→16 with verified backup; expired record payloads and pruned binding/inputs/observations; byte-identical closure and receipt; changed identity, generation and caller refused. This existing settlement behavior already passed before production edits. |
| RSF-S7-3 | `tests.test_rsf_s7_project`: bound and unbound settlement outboxes; altered digest before pruning; original request, request ID, attempt and generation controls after schema18→20, attempt pruning and two reopenings; closure replay and refusal to take a released identity again; historical settlement-table-only evidence copied before pruning; missing historical proof refused. |
| RSF-S7-4 | `tests.test_rsf_s7_journal`: real v1/v2 journal files read by a successor, strict retention boundary, actual removal, then replay with no journal or attempt payload; committed result-body expiry through `expire_results`, followed by journal and attempt pruning; immutable replies and no reacquisition. |
| RSF-S7-5/6 | All three modules plus `tests.test_rsf_s7_regressions` and `tests.test_reply_shape_compatibility`; exact-layout migrations, verified account backup, original assertion compatibility, unchanged reply fields, recorded red/green runs. |

`known` changes from False to True on a replay; all original answer fields and
authenticated closure bytes stay identical. “Exactly once” is the release and
identity transition: repeated replies are allowed and leave rows unchanged.
Snapshot comparisons include generations, receipts and evidence; account tests
also assert the single released slot/binding and absence of expired payloads.

## Red before production edits

Acceptance was written before the new tests. No production source was changed
until all three modules reached the behavioral failures below.

| Command | Result |
| --- | --- |
| `python3 -B -m unittest tests.test_rsf_s7_account` | **RED:** 2 tests, 2 failing generation subtests, **0.126 s**. The original digest with generations 2 and 3 incorrectly returned the recorded withdrawal. The sealed settlement/pruning control passed. |
| `python3 -B -m unittest tests.test_rsf_s7_project` | **RED:** 2 tests, 2 failures and 1 error across bound/unbound subtests, **0.050 s**. Changed settled-request digests were accepted; a pruned bound original raised `history_result_not_reserved`. |
| `python3 -B -m unittest tests.test_rsf_s7_journal` | **RED:** 1 test, 1 error, **0.021 s**. Both journal versions really expired, then the original bound request raised `history_result_not_reserved`. |

Earlier fixture-only attempts, also before production edits, are not red proof:
account **0.002 s** (state placement guard), **0.091 s** (backup identifier);
journal **0.013 s**, **0.012 s** (invalid synthetic token/container name).
The project behavioral red above passed fixture setup on its first run.
Fixture corrections changed no assertion.

## Implementation and final green

`learning_store.withdraw_open` checks a supplied generation against the original
request's recorded generation before replaying its receipt. It preserves the
existing digest-only `generation=None` compatibility.

`project_history` checks settled outbox request IDs and digests, keeps the
verified canonical closure in the released hold, and uses its authenticated
binding to find the original bound attempt after payload pruning. Before
deleting older settlement rows, pruning copies their already-verified closure
to the permanent hold. Existing tables and journal formats remain unchanged.
Missing historical replay proof is refused. Lookups use existing primary keys.

| Command | Final result |
| --- | --- |
| `python3 -B -m unittest tests.test_rsf_s7_account` | **4 passed, 0.225 s** |
| `python3 -B -m unittest tests.test_rsf_s7_project` | **4 passed, 0.084 s** |
| `python3 -B -m unittest tests.test_rsf_s7_journal` | **2 passed, 0.044 s** |
| `python3 -B -m unittest tests.test_rsf_s7_regressions` | **71 passed, 0.854 s** |
| `python3 -B -m unittest tests.test_reply_shape_compatibility` | **3 passed, 0.003 s** |

Final green total: **84 tests, 1.210 s** of unittest execution. Earlier green
iterations were account 2/0.152 s, 4/0.223 s, 4/0.240 s; project 2/0.062 s,
4/0.076 s, 4/0.089 s; journal 1/0.025 s, 1/0.024 s.

The regression wrapper runs all classes defined by `test_account_settlement`
and `test_project_holds_schema.SchemaStepTests`, preserving their original
assertions. It wraps the suite with worktree-only temporary directories and
guards that reject subprocesses and sockets. The account and project fixtures
also reject subprocesses and sockets. The account fixture overrides only the
state placement guard, because product state normally must be outside a Git
working tree whereas this task permits writes only inside this worktree.
Account rows are synthetic SQLite metadata with real product seals; no native
authorization, process execution or worker admission is claimed.

**Constraint exception:** the first regression-wrapper run passed 71 tests in
**0.864 s**, but its module setup was skipped for imported classes, so the
existing fixtures used their system temporary directories outside this worktree.
Their normal cleanup removed those temporary files. This run is not counted
as compliant validation. Moving the boundary to `TestSuite.run` corrected the
wrapper; the next run (71 tests, **0.846 s**) had two placement-guard errors in
`CollectorPaths`, while staying in worktree scratch. Adding the same fixture-only
placement override there produced the final 71/0.854 s green run above. No
existing test or assertion was edited to resolve these fixture issues.

## Socket / CLI follow-ups — not run

These exact commands require a future environment that permits git-backed
integration fixtures, subprocesses and sockets. Run each separately from
`packages/codex-agents`, with a 300-second bound. They are not passing evidence
for this no-socket task; existing modules may need dedicated S7 integration
cases to exercise migration and pruning through transport.

| Boundary | Command | Intended follow-up |
| --- | --- | --- |
| Authenticated withdrawal socket/CLI | `python3 -B -m unittest tests.test_open_withdrawal` | Original authenticated request after upgrade and retention; altered digest/generation/caller refused. |
| Successor withdrawal | `python3 -B -m unittest tests.test_open_withdrawal_successor` | Restart after a lost reply and pruned launch data; original receipt with one release. |
| Lost-run service/journal integration | `python3 -B -m unittest tests.test_lost_run_settlement` | Real service restart, journal retention and account/project settlement replay. |
| CLI status / collector sockets | `python3 -B -m unittest tests.test_cli_status_client_boundary` | Inspect retained identity/release after payload expiry without causing another transition. |
| Live reply compatibility | `python3 -B -m unittest tests.test_reply_shape_live` | Confirm unchanged account/history reply shapes through actual transports. |

Scope excludes old-writer fencing, resumable migration batches, new original-open
retention policy and a launch-journal format conversion. Historical tombstones
whose authenticated reply data was already lost cannot have it reconstructed.

Primary note (2026-11-25): tests.test_rsf_s7_regressions was removed before commit. It re-ran test_account_settlement and
SchemaStepTests through load_tests (a sandbox workaround), which made the runner discover those tests twice. They run in their
own modules; RSF-S7-5/6 evidence = those modules + test_reply_shape_compatibility, run by the primary (all OK).
