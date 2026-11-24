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
| RSF-S7-5/6 | All three modules plus `tests.test_reply_shape_compatibility`; exact-layout migrations, verified account backup, unchanged reply fields, recorded red/green runs. The primary separately ran `tests.test_account_settlement` and `tests.test_project_holds_schema` with original assertions. |

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
| `python3 -B -m unittest tests.test_reply_shape_compatibility` | **3 passed, 0.003 s** |

Retained S7/reply-shape green evidence: **13 tests, 0.356 s** of unittest execution. Earlier green
iterations were account 2/0.152 s, 4/0.223 s, 4/0.240 s; project 2/0.062 s,
4/0.076 s, 4/0.089 s; journal 1/0.025 s, 1/0.024 s.

The account and project fixtures reject subprocesses and sockets and use
worktree-only temporary directories. The account fixture overrides only the
state placement guard, because product state normally must be outside a Git
working tree whereas this task permits writes only inside this worktree.
Account rows are synthetic SQLite metadata with real product seals; no native
authorization, process execution or worker admission is claimed.

**Historical constraint exception:** an earlier, discarded wrapper run passed 71 tests in
**0.864 s**, but its module setup was skipped for imported classes, so the
existing fixtures used their system temporary directories outside this worktree.
Their normal cleanup removed those temporary files. This run is not counted
as compliant validation. The wrapper was removed before commit because it made
discovery run imported tests twice; none of its runs are included above.

## Review corrections (2026-11-25)

Each correction below added a new test and ran the named module red before
changing the implementation or documentation, then green. No existing
assertion was changed. All commands ran from `packages/codex-agents`, stayed
inside the worktree and completed in under 300 seconds; no git or sockets.

Before decision 214, RSF-S7-3/4 were interpreted as requiring permanent replay.
That interpretation and the unbounded evidence policy below are superseded by
the bounded-window correction recorded next.
`project_history.py::record_account_settlement` now has a lifetime-cap registry
entry explicitly distinguishing the inclusive 8,192-byte closure ceiling from
unbounded total growth, at most one closure per settled attempt. The new test
moves a valid authenticated closure across the byte boundary, verifies no
writes on refusal, and checks retained bytes after pruning/reopening.

`_delete_attempt_rows` now backfills only NULL or the exact adopted marker.
The new test covers prior evidence, NULL and adopted evidence for both bound
and unbound attempts, checking every hold field and settlement-row removal.

The optional withdrawal-message normalization was skipped: existing
`tests.test_run_holds` tests
`test_RS_A_F13_a_stale_generation_is_refused_and_changes_nothing` and
`test_RS_A_F13_an_identical_retry_without_a_generation_gets_the_stored_receipt`
pin `stale` and `generation_required` after a withdrawal. Those assertions and
refusal messages are preserved. The module was inspected, not run in this
constrained round. A new documentation test excludes citations of the deleted
module; its command row and totals have been corrected above.

| Command | Red → green evidence |
| --- | --- |
| `python3 -B -m unittest tests.test_rsf_s7_project` | Closure registry: **5 tests, 1 failure, 0.087 s** → **5 passed, 0.084 s**. |
| `python3 -B -m unittest tests.test_rsf_s7_project` | Evidence preservation: **6 tests, 2 failing subtests, 0.150 s** → **6 passed, 0.139 s**. |
| `python3 -B -m unittest tests.test_rsf_s7_project` | Deleted-module citations: **7 tests, 1 failure, 0.145 s** → **7 passed, 0.145 s**. |

Final correction checks (20 tests, 1.049 s of unittest execution):

| Command | Result |
| --- | --- |
| `python3 -B -m unittest tests.test_rsf_s7_project` | **7 passed, 0.145 s** |
| `python3 -B -m unittest tests.test_rsf_s7_account` | **4 passed, 0.233 s** |
| `python3 -B -m unittest tests.test_rsf_s7_journal` | **2 passed, 0.041 s** |
| `python3 -B -m unittest tests.test_reply_shape_compatibility` | **3 passed, 0.003 s** |
| `python3 -B -m unittest tests.test_lifetime_cap_guard` | **4 passed, 0.627 s** |

## Decision 214 — bounded retained closure evidence (2026-11-25)

Acceptance in `rsf-s7.md` was updated first. Three new tests were appended to
`tests.test_rsf_s7_project`; no existing assertion was edited. The named module
ran red before production changes: **10 tests, 5 failing subtests/tests and
1 registry error, 0.219 s**. Both bound/unbound expired replays still returned
answers, the cleanup pass cleared zero evidence rows, and the boundary registry
field was absent. Just-inside and exact-deadline replay checks already passed.

`project_history.SETTLEMENT_REPLAY_WINDOW_MS` derives the window from
`max(MAX_RESULT_RETENTION_MS, TOMBSTONE_MS) + TOMBSTONE_MS`, with the source
constant references recorded in `rsf-s7.md`. Both replay paths refuse strictly
after the hold release plus that window with `history_result_not_reserved`,
including when the settlement-table copy still exists and before cleanup.
`expire_results` runs a dedicated evidence sweep alongside its existing purges:
at most `limit * PURGE_SCAN_FACTOR` scalar candidate positions read and `limit`
evidence rows cleared per pass, with a cursor. Only `project_holds.evidence`
changes; hold rows, receipts, request identities and release fields remain.
Late attempt pruning cannot backfill expired evidence. No schema change.

`test_retained_closure_window_boundary_and_fixed_refusal` covers bound and
unbound evidence at deadline minus one, at the inclusive deadline, and after
it; both reply forms; refusal without writes before/after clearing; reopening;
clock rollback against the persisted expiry floor; and permanent hold identity.
`test_evidence_expiry_pass_is_bounded_and_eventually_clears_all_rows` clears
three bound/unbound closures over three passes of limit one, preserving all
other hold fields and unrelated evidence. The third new test covers expired
settlement copies and refusal to backfill historical missing evidence.

The registry retains the existing byte-ceiling test key and registers the new
boundary/purge tests separately. Its literal “no total bound” phrase now names
the explicitly rejected former policy, preserving the existing string assertion
while documenting finite evidence retention.

All commands below ran from `packages/codex-agents` with a 300-second bound,
`-B`, and worktree-local fixture scratch directories. No git, CLI or socket
operations ran. Final checks: **23 tests passed, 1.125 s** of unittest execution.

| Command | Result |
| --- | --- |
| `python3 -B -m unittest tests.test_rsf_s7_project` | Red: **10 tests, 5 failures, 1 error, 0.219 s** → green: **10 passed, 0.214 s**. |
| `python3 -B -m unittest tests.test_rsf_s7_account` | **4 passed, 0.225 s** |
| `python3 -B -m unittest tests.test_rsf_s7_journal` | **2 passed, 0.039 s** |
| `python3 -B -m unittest tests.test_reply_shape_compatibility` | **3 passed, 0.003 s** |
| `python3 -B -m unittest tests.test_lifetime_cap_guard` | **4 passed, 0.644 s** |

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

Primary note (2026-11-25): `tests.test_account_settlement` and
`tests.test_project_holds_schema` run in their own modules; the primary reports
both plus `tests.test_reply_shape_compatibility` passed. Those separate runs
have no timings recorded here and are excluded from the timed total above.
