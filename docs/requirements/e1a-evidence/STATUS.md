# E1a evidence status

Requirements accepted before tests; base `230c2fc0` (0.8.62 candidate), branch
`feat/e1a-evidence`. Changes are left uncommitted under the sandbox rule.

**Evidence added; unrestricted signature-worker gate remains outstanding.**
The counts and timings in the original-round sections below are historical;
the following fix round records the current eleven-test file and replaces the
original recapture-control evidence.
Ten new tests pass with the explicitly selected supplementary signature adapter
(15.370 s). Five tests also pass with every runtime boundary unmodified (9.525 s). All
twelve broken-wiring controls produce exactly one assertion failure and zero
errors. These are evidence-only changes; no runtime fix, schema/record-shape
change, VERSION change or runtime-digest catalogue change was made.

## Fix round

Fixed scope: review `040-e1a-r1/RESULT-claude.md`, MED-1, MED-2, LOW-1 and LOW-2,
on `2d4a66f2` over base `230c2fc0` (VERSION 0.8.62). The exceptional-exit
requirement was made explicit in E1a-T3 and its authoritative registry before
adding the test. No runtime defect was demonstrated and no runtime file changed.

| Item / requirement | Change and test | Evidence this round |
| --- | --- | --- |
| MED-1 / E1a-T2 | `ClaimContentionTests.test_real_stale_recapture_busy_then_retry`; `recapture` now wraps `SignatureJobIntake.capture_admission`, converting only a real `sql_read_busy` into `ClaimError('claim_unavailable')`. Initial capture succeeds before the test introduces contention at stale recapture. `completion_reason` is untouched by this control. | Red first: exactly one failure at `assert_busy`'s exact reason assertion (`account_busy` versus `claim_unavailable`), zero errors, `intended_assertion=True`, 1.313 s. Mutation removed: passes in the 11-test supplementary green. |
| MED-2 / E1a-T4 | `ConcurrentRefusalEvidenceTests.test_event_coordinated_exact_counts_for_all_acquisition_paths_during_health`; all T4 barriers, event waits and joins now use 20 s. Its holder safety bound scales from 30 s to 120 s; other tests retain 5 s/30 s defaults. Coordination still uses events and three fixed waves. | Passes in the supplementary green. The `counts` control still finishes all waves and fails only the exact-count assertion, zero errors, `intended_assertion=True`, 6.934 s. No timed load loop or product budget changed. |
| LOW-1 / E1a-T4 | Same exact-count test: idle-probe comment explicitly says it adds no `read_call`; the eight one-per-wave labels omit `read_call`, which `expected()` adds as `3 * wave` from the three owner-thread SQL reads. | Expected dictionaries are identical before and after; final `read_call` remains 9. Same green/counts-red evidence as MED-2. |
| LOW-2 / E1a-T3 | New `RequestDeadlineEvidenceTests.test_raising_collector_frame_restores_deadline_and_next_frame_budget`: real `_handle` dispatch performs a real ledger read, then raises; assert the identical exception, prior deadline restoration, and a successful next frame with fresh actual lock budgets. | Red first: `collector-exception` restores only on successful exit, so it fails precisely at `exceptional collector frame leaked request deadline`, one failure, zero errors, `intended_assertion=True`, 0.061 s. Mutation removed: passes in the supplementary green; unmodified named-gate result below. |

The supplementary command `"$PY" -B scripts/e1a_evidence_controls.py
--direct-signatures` passes all 11 tests, zero failures/errors, 15.570 s. This
retains the signature-adapter limitation documented below. Controls were run
sequentially before the unmodified gate, without parallel test load.

Normal gate: `"$PY" -B scripts/parallel_tests.py --module
codex:tests.test_e1a_evidence --jobs 1` completed with `metadata_errors: []`,
11 tests, 113.546 s worker time (112.944 s unittest time). Six tests passed on
unmodified runtime: both provenance cases, all three request-deadline cases
(including the new exceptional frame), and T4 counts. The same five
signature-dependent cases failed completion and drain at their unchanged 10 s
bounds: ten assertion failures, no test errors. The module also exceeded its
60 s scenario budget. This reproduces the previously established sandbox-worker
limitation; it does not establish a green production-worker gate for E1a-T2/T3.
The module remains registered as scenario/integration; the new test is linked
reciprocally to E1a-T3. No new module or requirement ID was introduced. Syntax,
JSON and `git diff --check` passed; unit docs contain no absolute home paths or
raw logs. All runs finished; nothing is left running.

Assertion changes are paired in [assertions.md](assertions.md): 5 s → 20 s test
coordination bounds, identical count dictionaries after the label clarification,
and additive exceptional-frame assertions. No existing product-result assertion
was removed or weakened. Changed this round: the evidence test module, control
script, E1a requirement document/source JSON/catalogue, this status, and assertion
notes. VERSION and the runtime digest catalogue are unchanged. No commit under
the sandbox rule.

No full suite, copier/backup/move/enumeration campaign, release-digest test or
live socket/PTY integration was run: there is no runtime/schema/record-shape
change, and the collector test uses a controlled connection at the real frame
handler. Production signature confinement still requires the normal named gate
on an unrestricted builder before accepting the five blocked worker cases.

## Requirement → test → evidence

All names below are in `tests.test_e1a_evidence`. “Supplementary” means that the
real ledger, lock holder, account methods, job coordinator, admission capture,
stale checks and retry execute, while private signature-helper confinement and
process-tree inspection use the explicitly selected test adapter described below.

| Requirement | Test | Green evidence | Red control |
| --- | --- | --- | --- |
| E1a-T2 | `ClaimContentionTests.test_claim_completion_busy_then_retry` | Supplementary; real completion refuses, no claim/receipt, healthy lane, retry claims once | `claim-completion`; additionally `claim-lane` catches a spurious failure note |
| E1a-T2 | `ClaimContentionTests.test_real_stale_recapture_busy_then_retry` | Supplementary; a genuinely new signed attestation causes recapture, whose real lock wait refuses; retry claims | `recapture` |
| E1a-T2 | `OpenContentionTests.test_initial_open_real_host_authorization_busy_then_retry` | Supplementary; product `AccountServiceHost._authorize`, valid synthetic enrollment, exact `project_for_root` refusal count, no pending open, healthy lane, retry opens | `initial-open` |
| E1a-T2 | `OpenContentionTests.test_open_completion_busy_then_retry` | Supplementary; real finish preserves pending journal, healthy lane, retry opens | `open-completion` |
| E1a-T2 | `ProvenanceContentionTests.test_open_provenance_real_contention_then_retry` | Unmodified runtime; valid sealed evidence, real adapter, healthy bootstrapped lane, unchanged database, identical proof on retry | `open-provenance` |
| E1a-T2 | `ProvenanceContentionTests.test_run_provenance_real_contention_then_retry` | Unmodified runtime; valid registered run, real adapter, healthy bootstrapped lane, unchanged database, identical proof on retry | `run-provenance` |
| E1a-T3 | `OpenContentionTests.test_exhausted_deferred_publication_restores_next_request_and_owner_pump` | Supplementary; commit precedes publication; actual publication `read_call` asks for zero seconds after exhaustion; restoration, next `begin`, real owner pump/read and known-commit retry succeed | `publication`; additionally `restoration` catches a leaked deadline |
| E1a-T3 | `RequestDeadlineEvidenceTests.test_real_handle_and_begin_share_successive_waits_and_refresh` | Unmodified runtime; both entries actually acquire the real lock twice, requesting 0.75 then 0.30 seconds; each new request refreshes and restores | `account-entry` |
| E1a-T3 | `RequestDeadlineEvidenceTests.test_slow_collector_receipt_and_next_frame_get_fresh_budgets` | Unmodified runtime; actual collector `_handle`, controlled split receipt takes 1.2 s; each frame's dispatch acquires with 0.75 then 0.30 seconds, proving the budget starts after receipt | `collector-frame` |
| E1a-T4 | `ConcurrentRefusalEvidenceTests.test_event_coordinated_exact_counts_for_all_acquisition_paths_during_health` | Unmodified runtime; three event/barrier waves, exact health-published dictionary each wave, all workers acknowledged, retry succeeds | `counts` |

Every T2 busy assertion validates the decoded reason, complete wire failure
shape, request digest and exact plain guidance. No fake authorizer raises busy:
the refusals all originate from the real ledger lock held by another thread.
Provenance uses synthetic project authorization/evidence custody; initial-open
authorization uses the real host authorizer, including its real project lookup
and original-open-context decoder.

T4 covers default/explicit `_held`, `_write_unit`, `_account_transaction`, reader
entry, slot health, sweeps, project health, and all three SQL scope entry methods
(`read_call`, `transaction_read_call`, `read_in_current_mode`). The latter run on
the actual connection owner thread. A separate thread reads ledger-free health
after a real refusal has counted and before refusers may complete. The final
dictionary is exactly:

```json
{"held_default":3,"explicit":3,"write_unit":3,"account_transaction":3,"repository":3,"slot_health":3,"sweeps":3,"health":3,"read_call":9}
```

The opportunistic `_holds_due_idle` acquisition also runs and skips without
counting, as designed. There is no timed contention/load loop. Barrier and event
timeouts only detect a stuck test. Existing rollback/reader-maintenance-race
assertions in `test_ledger_lock_limits` remain unchanged and were not rerun.

## Broken-wiring controls

`scripts/e1a_evidence_controls.py` changes wiring only in memory, then runs the
specified named test. A red control succeeds as a harness only for exactly one
assertion failure at its designated assertion with no errors; worker/holder timeouts do not count as evidence.
Restoration of patches is automatic. Each matched test passes again without its
mutation in the final ten-test supplementary green run.

| Control | Mutation and assertion detected | Seconds | Signature adapter |
| --- | --- | ---: | --- |
| `claim-completion` | Drop busy completion mapping → exact reason mismatch | 1.807 | Supplementary |
| `claim-lane` | Treat busy as a completion fault → unexpected lane failure note | 1.807 | Supplementary |
| `recapture` | Drop busy recapture mapping → exact reason mismatch | 1.695 | Supplementary |
| `initial-open` | Host authorizer swallows real SQL busy → authorization reason mismatch | 1.705 | Supplementary |
| `open-completion` | Real finish remaps busy to `open_failed` → exact reason mismatch | 1.828 | Supplementary |
| `open-provenance` | Disable busy pass-through → exact reason mismatch | 1.662 | None |
| `run-provenance` | Disable busy pass-through → exact reason mismatch | 1.851 | None |
| `publication` | Omit deferred-publication request wrapper → actual SQL timeout is 0.75 instead of zero | 2.101 | Supplementary |
| `restoration` | Set but never restore request deadline → thread-local deadline remains set | 1.175 | Supplementary |
| `account-entry` | Omit shared handle/begin wrapper → second acquisition gets 0.75 instead of 0.30 | 0.096 | None |
| `collector-frame` | Omit frame request wrapper → successive acquisition budgets mismatch | 0.111 | None |
| `counts` | Drop only SQL read refusal increments → health-published per-wave dictionary mismatch | 7.014 | None |

The final control series took 33.075 s. Every control reported
`intended_assertion=True`, exactly one failure and zero errors; the times in the
table are from that final series. The complete supplementary green on the final
test file took 15.370 s. No runtime code was changed between red and green.

## Reproduction

Set `PY` to the test virtualenv interpreter from the builder handoff. From the
repository root, run the normal gate on an unrestricted builder:

```sh
"$PY" -B scripts/parallel_tests.py --module codex:tests.test_e1a_evidence
```

Registration is scenario/integration, including the process-starting fixtures.
The named runner was executed here: `metadata_errors: []`, ten tests discovered,
108.835 s worker time. Five tests passed and five signature-dependent tests timed
out before the required worker completion/retry. Those five each also failed
their existing worker-drain assertion (ten failure entries altogether). The
timeouts also exceed the 60 s scenario budget; no waiver or budget relaxation
was added. The final expanded T4 case is covered by the unmodified selected run
and controls, rather than rerunning this known-blocked 108 s worker campaign.

The original baseline test
`tests.test_account_claim_jobs.AccountClaimJobTests.test_actual_deferred_claim_verifies_off_owner_and_uses_original_atomic_receipt`
also failed completion and drain at the unchanged ten-second limits (20.630 s).
A behavior-preserving observation reproduced it (20.405 s): the private helper
exited 71, and its execution was retained as unconfirmed. Independently, even
`sandbox-exec` running `true` exits 71 here because `sandbox_apply` is denied.
Process inspection is also denied. This establishes an environment prerequisite
failure; it is not a successful production-worker evidence run.

Supplementary verification and a sample negative control:

```sh
"$PY" -B scripts/e1a_evidence_controls.py --direct-signatures
"$PY" -B scripts/e1a_evidence_controls.py --direct-signatures --control claim-completion
"$PY" -B scripts/e1a_evidence_controls.py --control counts
```

The optional adapter runs the real payload preparer in a child process and real
SSH public-key verification for every signature. It waits for the actual child
handles and reports their actual count; pool result validation stays unchanged.
It intentionally does not establish the product helper's confinement, process
birth or descendant-cleanup guarantees. The ordinary tests do not select this
adapter, and the normal runner receives no such patch.

To run the five independent tests unchanged, from `packages/codex-agents`, with
`scripts` added to `PYTHONPATH` and a short private temporary directory:

```sh
"$PY" -B -m unittest tests.test_e1a_evidence \
  -k provenance -k real_handle -k slow_collector -k event_coordinated -v
```

Syntax, JSON and `git diff --check` passed. No full suite, socket/PTY-hosted
integration, platform isolation campaign, release-digest test or copier/backup
campaign was run: no runtime/schema changes require those, and installed services
are outside this unit. The collector receipt test uses a controlled connection
object at the real `_handle` boundary, not a live socket.

## Changed files and handoff

- `packages/codex-agents/tests/test_e1a_evidence.py`: ten additive evidence tests.
- `scripts/e1a_evidence_controls.py`: reproducible negative-control harness and
  explicitly optional supplementary verifier.
- `scripts/test-layers.json`, `test-stages.json`, `test-timings.json`,
  `test-requirements.json`: module and requirement registration.
- `tests/requirements/e1a-evidence.json`: authoritative registry source.
- `docs/requirements/e1a-evidence.md`, this status and
  [assertions.md](assertions.md): requirements, evidence, draft-assertion corrections.

No base-tree assertion was changed or removed. Draft corrections are paired
old → replacement in `assertions.md`. No production defect was demonstrated, so
no runtime fix is proposed. Primary action: run the normal named gate where
signature-helper confinement/process inspection are available before accepting
the remaining five production-worker cases. Nothing is left running.
