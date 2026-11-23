# B3 test lines changed (packages/codex-agents/tests/test_open_withdrawal.py)

Base: commit 2cac1726 (the 48 tests as first written). No assertion was changed, removed or weakened. Three groups, in
three commits so the primary can accept or revert each on its own.

## 1. Approved correction: canonical run ids (commit "tests")

Production's canonical run id is `run:<64 lowercase hex>`; the module's `WithdrawOpenInput` rightly rejects `run-1`.

| old line (2cac1726) | new line | change | why |
|---|---|---|---|
| 30 `from tests.test_project_history import PROJECT_A, binding` | 32 `... PROJECT_A, binding as history_binding` | import renamed | the module defines its own `binding` |
| (none) | 16-17 `import dataclasses`, `import hashlib` | added | the helper below |
| (none) | 59-62 `def binding(number=1)` returning `dataclasses.replace(history_binding(number), run_id='run:' + sha256('run-<n>').hexdigest())` | added | every use of `binding(n)` in this module (30 call sites, unchanged) now carries a canonical run id |
| (none) | 72, 151, 352, 513, 615 `@test_framework.requirements('RS-B-5', ...)` | added (Codex pass) | framework registration of the five classes |
| (none) | M7 tests, `DriverWithdrawalTests` (two new methods) | added | row M7 had no test |

## 2. NOT covered by the approval: enrollable host class names (commit "tests: host class constants")

`withdraw_as` enrolled with `host_client='codex'` / `'claude'`. `enrollment_preamble` (service_enrollment.py:298) only
accepts `HOST_CLIENTS` (`codex-host-v1`, `claude-host-v1`) or the owner client, so every `withdraw_as` call raised
`EnrollmentError` inside the test before reaching the account. Same family as the run id (a fixture value the real
system cannot accept), but not the correction the owner delegated: the primary must approve it.

| line | old | new |
|---|---|---|
| 41 | `from codex_agents.hosts import OWNER_CLIENT` | `... import CLAUDE_HOST_CLIENT, CODEX_HOST_CLIENT, OWNER_CLIENT` |
| 314 | `host_client='codex'` (default of `withdraw_as`) | `host_client=CODEX_HOST_CLIENT` |
| 398, 399, 450, 451 | `host_client='claude'` | `host_client=CLAUDE_HOST_CLIENT` |

The store tests' `'codex'` string for `record_open_binding(host_client=...)` (lines 171, 177, 253) is left as it was:
the store treats it as a label.

## 3. NOT covered by the approval: W3 withdrawal-first hold state (commit "tests: W3 hold state")

| line | old | new |
|---|---|---|
| 497 | `self.held_at(lambda driver, run: run.state == 'observing')` | `... run.state == 'recording_open'` |

Why: the test holds the run at `observing`, which is while the provider observation is still in flight; the
withdrawal lands first and the observation is refused (`observation_not_authorized`), so the run fails before it takes
a lease and the test's own comment ("the run's own lease was armed ... released `fenced`") can never hold. At
`recording_open` the lease is armed and the claim is then refused, which is the scenario the test describes and the
design row W3 ("withdrawal first: the claim refused"). All assertions are unchanged.

## 4. Added test (condition of the group 3 approval)

`DriverWithdrawalTests.test_W3_withdrawal_while_the_provider_observation_is_in_flight_fails_the_run_before_any_claim`
(new; the moved W3 test is untouched). It holds the run at `observing`, withdraws the open (with the reason the driver
would send, so the driver's own duty is an exact replay), then releases it. Asserts: the dispatch fails; the run's first
failure is `('observing', 'observation_not_authorized')`; no model execution; no lease row (so no claim was sent); the
open is withdrawn exactly once (one non-`known` transition); the project row ends `released`/`withdrawn`. Passes.

## 5. Fixes after the two security reviews (branch fix/b3-withdraw-2)

### 5a. Moved, no assertion change (item 5, test time)

`SuccessorWithdrawalTests` (class body, decorator, docstring and its one test unchanged, `LostRunAfterStopTests` still
its base) moved from `tests/test_open_withdrawal.py` to the new `tests/test_open_withdrawal_successor.py`. In
`test_open_withdrawal.py` only these lines changed: the import of `LostRunAfterStopTests` was removed, the class was
removed, and `load_tests` no longer lists it. The new module has its own `load_tests` (this class only) and imports
`WithdrawReads` from `tests.test_open_withdrawal`. Registered in `scripts/test-stages.json` (integration),
`scripts/test-layers.json` (scenario) and `scripts/test-requirements.json` (RS-B-5 check ids renamed to the new module,
regenerated with `generate_test_metadata.py --catalogue`).

### 5b. Added tests (no existing line changed), red on 0e9dae86 before the fixes

All in `tests/test_open_withdrawal.py` (plus `_CountedConnection`/`_CountedCursor` helpers above `OpenBindingStoreTests`):

| test | red evidence on 0e9dae86 |
|---|---|
| `DriverWithdrawalTests.test_M1_a_withdrawal_the_account_keeps_refusing_does_not_block_a_later_one` | the later withdrawal never happened in 15 s (the driver kept retrying the first refused row) |
| `OpenBindingStoreTests.test_M1_a_withdrawal_is_listed_only_when_due_and_each_failure_waits_twice_as_long` | `open_withdrawals()` takes no `now_ms`; no per-row deferral exists |
| `OpenBindingStoreTests.test_M2_the_successor_pass_reads_at_most_its_limit_of_rows_and_every_row_is_handled` | 35 rows read with limit 10 |
| `OpenBindingStoreTests.test_M2_a_registration_reads_one_row_however_many_are_held_and_still_refuses_a_second_owner` | 30 rows read to register one run |
| `WithdrawAccountTests.test_L1_the_permanent_withdrawal_evidence_stays_within_its_512_byte_budget_at_its_largest` | stored evidence 668 bytes > 512 |
| `WithdrawAccountTests.test_L1_a_withdrawal_recorded_in_the_earlier_format_still_replays` | green before and after: guards the v2 record still read |
