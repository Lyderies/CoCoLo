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
