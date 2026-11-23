# D4 fix round 2

Baseline: `e4cda541`, branch `feat/d4-replay-recovery`. Read all of `REVIEW-d4-r2c.md`, round-one rules/evidence, the D4 requirements and RS-D-D4 registration. Finding IDs below follow the six rows of the review's verdict-and-fixes table.

Tests were written and run red before fixes (`red-unit-r2.log`). The final regression bodies were also run with both production files, the driver observer module and lifetime registry temporarily restored from `git show e4cda541:<path>`; every working file was restored in `finally`. That repeat (`red-final-baseline-r2.log`) ran 51 tests in 2.761 s: 13 failures (including subcases), two errors. Both errors are expected legacy arbitration refusals: `history_conflict` with a source lease and `history_result_expired` without one. For verification-only findings, red means the original observer accepts a deliberately injected bad sequence, so its sensitivity witness fails.

Paths below abbreviate `packages/codex-agents/native-package/.codex/orchestration/codex_agents/` as runtime and `packages/codex-agents/tests/` as tests. All named tests are in the two authorized socket-free modules. Every row is green in `green-unit-r2.log`: 51 tests pass in 1.307 s, proofs first to exercise observer cleanup before the real store tests.

| Finding | Test | Red on e4cda541 | Fix file:line | Green |
|---|---|---|---|---|
| F1 HIGH: live publication accepts a missing lease and hold | `test_publication_replay.ReviewRegressionTests.test_bound_context_without_hold_or_lease_cannot_replay` | Pre-expiry `commit_result` raises nothing; expected `HistoryError` | runtime `project_history.py:2327`: query the actual reservation hold and require `held`; keep the authenticated committed-retry fast path | Exact `history_result_expired`; unchanged custody/context/material/digests/files/charge/counts; original late direct-replay refusal retained |
| F2 MED: stopped migrated row cannot arbitrate absence | `test_publication_replay.ReviewRegressionTests.test_migrated_reserved_row_without_material_can_be_fenced_without_bound_context` | Actual 17-to-18 migration creates `legacy_no_context`; arbitration refuses both lease-born and reservation-only rows | runtime `project_history.py:2530`: after the actual hold/settlement checks, arbitration recognizes absent material independently of replay eligibility | Both direct replays still refuse; arbitration installs `releasing` with the request identity, reservation unchanged, repeat returns `settling`, no terminal |
| F3 MED: retry during adoption defeats installation | `test_publication_replay.ReviewRegressionTests.test_retry_during_adoption_attestation_preserves_the_installed_delivery`; `test_stale_queued_publish_leaves_an_already_installed_delivery_alone` | Retry synchronized inside attestation leaves installed job `failed/history_service_unavailable`; independent stale queued publish also leaves it failed | runtime `result_persistence.py:627`, `:1121`, `:1305`: claim `adopting` under retry's lock, release it for store/attestation, restore `failed` on retryable adoption failure, and skip an installed delivery's stale publish; do not adopt jobs that already own a delivery | Real writer queue/dispatch preserves committed state, exact delivery and evidence, no extra `_publish`; existing later-expiry retry and equal-install identity tests also pass |
| F4 MED: hidden wrong-target settlement / empty first sweep | `test_publication_replay_proofs.PublicationProofSensitivityTests.test_settlement_observer_retains_wrong_run_and_claim_calls`; `test_sweep_first_observer_rejects_settlement_only_after_expiry` | Baseline scenario accepts wrong run and wrong claim calls followed by a valid call; also accepts an empty sweep followed by expiry and later settlement | tests `test_publication_replay_driver.py:328`, `:389`, `:400`: record call identity first, validate all identities on the test thread, require the target return within the first sweep in sweep-first mode | Both injected bad sequences rejected, valid first-sweep control accepted; existing all-call commitment/completion tests pass |
| F5 MED: inherited leak assertion lost / cleanup exceeds deadline | `test_publication_replay_proofs.PublicationProofSensitivityTests.test_driver_teardown_still_rejects_each_inherited_named_worker_leak`; `test_registered_cleanup_obeys_the_shared_deadline_even_after_teardown_fails` | All four named-worker leaks accepted; cleanup joins for 20 s after teardown consumes the remaining 0.5 s | tests `test_publication_replay_driver.py:61`, `:64`: restore inherited worker assertion and override the separately registered cleanup to cap every join at the shared deadline | Each leaked worker rejected, clean control accepted; joins are `[0.5, 0]` and the simulated module ends at exactly 60 s |
| F6 MED: missing D3 cap registrations | `test_publication_replay_proofs.PublicationProofSensitivityTests.test_lifetime_registry_keeps_both_D3_entries_and_D4_replay` | Both D3 entries are absent | tests `lifetime_caps.json:1479`, `:1484`: restore `context_rows` and `reservation_rows` exactly from `049e2ee8`; retain D4's entry | Both complete entry values equal baseline; replay entry still present |

The migration fixture uses real schema upgrade code for both a lease-born in-flight row and a reservation-only prepared row. `red-legacy-reference-r2.log` catches the second class against the first, context-only fix; `red-registered-legacy-r2.log` is the superseded registered run that also caught it before the final arbitration fix. The final fix reads absence only after verifying the actual reservation hold, so it covers both classes without relaxing replay or reading a body twice. Its legacy-format preparation removes claim 4's publication material written by today's fixture setup, since format 17 never had that table. No pre-D4 test method is run or modified.

`red-installed-adoption-r2.log` additionally catches an installed job whose later acknowledgement failed being left in `adopting`; the delivery-presence guard keeps that job untouched for acknowledgement retry. The adoption handoff adds no lock across database/file/attestation work and no accumulating collection or new numeric limit. The existing bounded live job owns its adoption state and retry trigger. A failed attestation restores `failed` with its selected packet and trigger intact so the existing later expiry test still succeeds.

All replaced assertions are mapped in `assertions-r2.md` (233 baseline calls, two mapped replacements, 231 unchanged). All baseline D4 test names remain. No pre-D4 Python test/fixture changed; the requested registry restoration is the only changed baseline `tests/` file. No full suite, socket-backed driver execution or commit was attempted. Changes remain in the worktree.

## Commands and primary follow-up

All Python execution uses the requested interpreter:

```sh
PY=<test-venv>/bin/python
# From packages/codex-agents (red used the opposite module order):
PYTHONPATH=../../scripts "$PY" -B -m unittest -v tests.test_publication_replay_proofs tests.test_publication_replay
# From the repository root, only the two authorized modules:
"$PY" -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module codex:tests.test_publication_replay --module codex:tests.test_publication_replay_proofs
```

The primary must run the seven socket-backed driver cases and measure the aggregate module against its shared 60-second budget:

```sh
"$PY" -B scripts/parallel_tests.py --base b531844030fa619c1f62ddd6c092ebc2ea4fca38 --module codex:tests.test_publication_replay_driver
```

## Final registered runner evidence

`green-registered-r2.log` selects only `codex:tests.test_publication_replay` and `codex:tests.test_publication_replay_proofs` using the requested interpreter and the round-one review base. Both units pass: **51 tests**, no failed units, no requirement failures and no budget failures. Module totals round to 1.9 s and 0.7 s (2.596 job-seconds). Discovery took 20.874 s and global preflight took 460.940 s, separately from module budgets. The runner exits 1 because of **2,085 global metadata diagnostics**; their exact set is identical to `green-registered-r1.log` (zero added or removed, none mention D4 replay). Existing unmeasured-module budget warnings do not require debt retirement in this selection. No unrelated suite was executed or altered to suppress diagnostics. `git diff --check` passes.
