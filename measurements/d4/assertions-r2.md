# D4 round-two assertion ledger

Compared with `e4cda541` on `feat/d4-replay-recovery`. An AST inventory grouped every explicit `assert*` call by its enclosing class/function (including nested callbacks), comparing each call with multiplicity. Of 233 original calls, 231 remain identical; the two replacements below preserve both identity checks and move them onto the test thread. The final inventory has 276 calls. All 50 baseline D4 test names remain (37 store, seven driver, six proofs); three store and five proof tests are added.

| Original assertion at e4cda541 | Replacement | Why the guarantee is preserved |
|---|---|---|
| `packages/codex-agents/tests/test_publication_replay_driver.py:320`, `stranded_then_successor.settling`: `self.assertEqual(run_handle, value.run_handle)` | Same scenario, line 404: `self.assertEqual(run_handle, event[2])` for every recorded settlement call | The callback records the actual run identity before reading state or calling the account. The test thread checks all calls, including wrong-target calls whose callback would previously raise inside the sweep and disappear. |
| `packages/codex-agents/tests/test_publication_replay_driver.py:321`, `stranded_then_successor.settling`: `self.assertEqual(held.binding.claim_id, value.bridge_run_id)` | Same scenario, line 405: `self.assertEqual(held.binding.claim_id, event[3].bridge_run_id)` for every recorded settlement call | The event retains the exact account input. Claim identity is still compared to the faulting publication's claim, after callback exceptions can no longer suppress observation. |

No other original assertion is removed or replaced. In particular:

- The combined missing-lease/missing-hold test keeps its original late direct-replay refusal. It additionally requires a pre-expiry live refusal with `history_result_expired` and unchanged records, files, prepared digest/retention/state, material, capacity charge, context and custody.
- The observer keeps every call/return, committed-state, target-run, record-count and final-state check. Sweep-first additionally requires a completed target settlement between the first `sweep_start` and first `sweep_done`.
- The driver teardown keeps its service joins, joined-thread assertions and failure-list assertion, and restores the inherited named-worker leak assertion from `service_harness.py:282`. Its separately registered cleanup now uses the same remaining module budget. The pre-D4 harness is unchanged.
- The cached-writer-callback proof keeps every assertion, including the required two observations and exact packet/time identity. Its synthetic pending job now explicitly has `delivery=None`, matching the real job field used by the stale-publication guard.
- Existing failed-attestation/later-expiry and equal-adoption identity/evidence proofs remain intact and pass alongside the new synchronized retry regression.

`red-unit-r2.log` records the regressions before any fix. `red-final-baseline-r2.log` repeats the final tests against `e4cda541` versions of both production files, the driver observer module and the registry, restoring all working files in `finally`: 51 tests, 13 failures and two errors. Verification-only reds are sensitivity witnesses: baseline observers accept the injected wrong identity, delayed settlement or leaked worker; these do not claim the injected service behavior existed in baseline production. `green-unit-r2.log` records all 51 socket-free tests passing.

The endpoint comparison against `049e2ee8` finds no changed pre-D4 Python test or fixture. The sole changed existing `tests/` file is the explicitly requested `lifetime_caps.json`: both D3 entries match that commit exactly and D4's replay entry is retained. New test checks are registered only under RS-D-D4 in `scripts/test-requirements.json`.
