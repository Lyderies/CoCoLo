# B4m validation

Only affected tests ran. Every Python invocation used `-B`; no git command, full suite,
VERSION edit, or runtime-releases.json edit was performed. No existing assertion changed.

## Results

| Run | Result | Seconds |
| --- | --- | --- |
| New tests on supplied 8d4a7ce3 base, before production changes | 8 tests; red, 6 failures and 51 errors (including subtests); captured rerun in `red.json` | 0.018; captured rerun 0.033 |
| Additional clone-registration test, before project-root plumbing | 1 test; red, missing `project_root` keyword | 0.003 |
| Initial new-test green | 8 tests passed | 0.287 |
| Affected regression selection, including all 9 new tests | 86 passed, 0 skipped; exact selection and socket exclusions in `green.json` | 6.500 |
| Further operation contracts: `test_open_withdrawal.WithdrawWireTests`, `test_account_open_schema.AccountOpenContractsTests`, `test_billing_probe_api`, `test_project_worker_result`, `test_project_qa_check_receipts`, `test_project_qa_commit.ProjectQACommitTests`, `test_project_qa_commit.ProjectNegativeQATests` | 87 passed; existing helper emitted ResourceWarnings for unclosed fake-launcher reads | 43.222 |
| `generate_test_metadata.py --catalogue` | Passed using `/private/tmp/cocolo-testfw-venv/bin/python3 -B`; system Python lacked Hypothesis | Discovery only |
| `parallel_tests.metadata()` | 0 errors; no tests executed | 14.529 |
| Ruff, four new Python files | Passed | Under 1 |
| Ruff, all touched Python files | Existing `learning_store.py` F401 for unused `CLAIM_DENIAL_CODES`; that import was not changed | Under 1 |

`RUNTIME_FILES`' exact file-set check passed. The current-version digest pin test was not run:
the requested slice changes runtime files while explicitly leaving VERSION/runtime-releases.json
for the primary's release bookkeeping.

## Socket checks for the primary

An initial affected API run attempted 74 tests in 7.203 seconds. Seven socket tests failed
because this sandbox refuses AF_UNIX listener binding (`PermissionError: [Errno 1] Operation
not permitted`). Four additional errors were the new binding fixture's incomplete manifest,
corrected before the successful 86-test run. The fixture correction changed no assertions.

Run from `packages/codex-agents` outside the socket-restricted sandbox:

```sh
python3 -B -m unittest \
  tests.test_account_api.AccountApiTests.test_dropped_or_partial_read_reply_is_not_automatically_retried \
  tests.test_account_api.AccountApiTests.test_real_socket_public_owner_reads_use_explicit_retry_and_survive_restart \
  tests.test_account_api.AccountApiTests.test_real_socket_reads_two_projects_one_owner_and_survive_restart \
  tests.test_history_api.HistoryProcessApiTests \
  tests.test_service_transport.ServiceTransportSocketTests.test_slow_reply_cannot_extend_the_deadline
```

The first seven tests were attempted and blocked; the final transport socket test was left
for the primary after the restriction was established. The changed CLI's existing real-driver
compatibility check also needs the primary's socket/git-capable harness:

```sh
python3 -B -m unittest \
  tests.test_project_driver.ProjectDriverTests.test_a_service_left_by_another_version_is_replaced_only_when_no_run_is_active \
  tests.test_project_driver.ClaudePrimaryDriverTests.test_a_service_left_by_another_version_is_replaced_only_when_no_run_is_active
```

No driver test was attempted here because its fixture also runs git.

## Files

Runtime changes: `.codex/orchestration/cli.py` and `codex_agents/{collector_client,learning_store,
account_api,history_api,runtime_files}.py`; new `codex_agents/{compatibility_message,reply_shapes}.py`
(all under `packages/codex-agents/native-package/`).

Tests: new `packages/codex-agents/tests/{test_b4m_compatibility,test_reply_shape_compatibility}.py`
and `tests/fixtures/reply-fields-8d4a7ce3.json`.

Documentation/evidence: `docs/troubleshooting.md`, `CHANGELOG.md`, `docs/requirements/b4m.md`,
`docs/requirements/b4m/{assertions.md,red.json,green.json,validation.md}`.

Registration: `scripts/{test-stages,test-layers,test-requirements}.json`.

## Primary run (socket-capable)

Socket tests, driver tests, route_*, account/history API, hold_lifecycle, cli_open_client_runtime,
container_launch_journal: all pass (about 4 s + 28 s + 47 s). A first run exposed one defect:
the `account.provider_state_snapshot` reply table listed 3 fields, but the real reply is the full
audit (classifier, evaluated_at_ms, format_version, project_id, providers, snapshot_ref, status).
Table and fixture corrected to the real 7 fields. No existing assertion changed.

## Round 2: reply tables proven against the real producers (cross-provider review, two MEDs)

Live coverage (`tests/test_reply_shape_live.py`). The tables are hand-written, so a wrong entry would
fail a live route closed. The new test wraps the real reply constructors of `AccountService` and
`HistoryService` (the billing path included) and records the operation and field set of every reply
that the existing driver modules make the real services produce (some over real sockets, most through
`handle()`; 3 module groups, listed in the test). It asserts: each recorded reply satisfies
`reply_fields_match`; every operation in both tables (all 21 account, all 25 history) produced at least
one reply (`SOURCE_ONLY` is empty: none had to be derived from source); every optional field
(billing_probe billing_waivers/client_version/retry_at, observe_provider billing_waivers,
capacity_report run_slots) was seen on a real reply; no real reply has a field outside its entry.
One deliberately fabricated reply (`account.project` canary in a refusal test) is excluded by name.
Table entries fixed in this round: none (the live run found the 46 entries correct after the earlier
`account.provider_state_snapshot` fix). Caveat: replies only come from what those modules exercise; a
new variant needs a driver, and the test fails for any operation that has none.

Fixture provenance. `tests/fixtures/reply-fields-8d4a7ce3.json` was cross-checked against the real
8d4a7ce3 producers, not the table: a read-only worktree at 8d4a7ce3 (no reply guard exists there) ran
the same driver modules with the same reply-constructor recorder, and the observed key sets per
operation were compared with the fixture (required = fields on every reply; optional = fields on some).
All 46 operations were observed and agree. Two optional fields were present on every reply the 8d4a7ce3
tests produced (`observe_provider.billing_waivers`, `capacity_report.run_slots`); the 8d4a7ce3 source
makes both conditional (`provider_observation_contracts.py` allows the key to be absent;
`history_service.py` adds run_slots only when the authorization carries them), so the fixture keeps them
optional. Recorder scripts were run from the scratchpad, not committed.

LOW: `runtime_upgrade_required` (newer client, older collector) now carries the same plain message and
exact update command as `runtime_newer` (`compatibility_message`), via `collector_client.health`.
New test `test_b4m_compatibility.CompatibilityMessageTests.test_newer_client_older_collector_names_command`
(red before the change: message was only "collector runtime is older than the installed package").
No assertion changed (no existing test matched the old text).

Round 2 runs: `test_reply_shape_live` 4 tests OK in 545.5 s (loaded machine, other units and a base-worktree recorder
running; it replays about 780 existing tests, so it is registered in the e2e stage, not for the per-edit loop);
`test_b4m_compatibility` + `test_service_transport` 19 tests OK in 0.3 s. The runtime digest/VERSION bookkeeping for the
collector_client.py change is left to the primary.
