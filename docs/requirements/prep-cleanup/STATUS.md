# Preparation cleanup status

Worktree branch: `feat/prep-cleanup`; base: `afb6f610` (0.8.66).
No commit attempted in the sandbox. VERSION and the digest catalogue are unchanged.

| Requirement | Test evidence | Current result |
| --- | --- | --- |
| PREPCLEAN-1 | `CodexPreparationTests.test_PREPCLEAN_1_missing_child_is_not_workspace_removal` was red (`True` incorrectly treated as proof), now green. `test_PREPCLEAN_1_twelve_physical_failures_then_observed_removal` observes each failed removal and the thirteenth removal, with reserved bytes and intake/retirement refusals before proof. | Direct real-worker tests pass. |
| PREPCLEAN-2 | `GivenBackPreparationTests.test_PREPCLEAN_2_retries_continue_past_twelve_with_capped_monotone_backoff` and `StuckSlotTests.test_M4_pending_cleanup_keeps_custody_and_defers_replacement` were red at the thirteenth due request; now pass. `CodexPreparationTests.test_PREPCLEAN_real_cleanup_keeps_host_and_slot_until_thirteenth_removal` was red at the host holding predicate; now observes real removal before releasing that predicate. | Capped pauses, no early work, exhaustion guard and physical hold pass. |
| PREPCLEAN-3 | `CleanupWarningTests.test_PREPCLEAN_3_plain_warning_and_clear` was red (missing warning), now green. Both `StatusWarningTests` tests were red (missing extractor/list entry), now green. `CodexPreparationTests.test_PREPCLEAN_3_capacity_transport_warning_and_clear` passes actual framing/decoding, warning/clear and invalid/past-bound count refusal. | Status transport and terminal-list unit checks pass. |
| RS-B M2/M5 | Unchanged `tests.test_memory_holds`, including `DriverMemoryTests.test_M2_unknown_cleanup_keeps_both_holds` and `test_M2_abandon_does_not_drop_entry_before_preparation_retirement`. | 20 tests pass. |
| Full automatic replacement lifecycle | `PreparationCleanupLifecycleTests.test_PREPCLEAN_pending_cleanup_defers_idle_replacement_then_removes_and_replaces` checks twelve physical failures, original host/folder/slot and both durable holds at the idle boundary, status warning, thirteenth removal, cleared warning/holds and replacement dispatch. | Service fixture cannot create its collector socket in this sandbox. No end-to-end pass claimed. |

First core named-module run: 154 tests, no failures, two existing skips;
19.19 job-seconds. Final core named-module run: 173 tests, 170 pass, two
existing skips and the one expected runtime catalogue mismatch; 17.67
job-seconds. Both runs report `metadata_errors: []`; no new test module was
introduced. The final run also passes setup terminal rendering and source guards.
The final preparation module (27 tests) includes the framed transport check.
An additional 27-test direct run (14.633 s) checks the actual host idle clock
past its replacement deadline: zero while cleanup is held, elapsed after
observed removal. Final lifetime-registry and wizard import-cleanup reruns
pass (4 tests/0.732 s and 53 tests/0.069 s respectively). The final retry
module rerun preserves the original initial-zero assertion and passes (27
tests, two existing skips, 0.163 s).

The runtime release failure is
`RuntimeReleaseTests.test_current_runtime_is_recorded_for_the_current_version`:
collector runtime bytes changed while release bookkeeping intentionally remains
at the supplied base. The primary owns the version/digest update at merge.

Compatibility/copy/backup/enumeration selection: 37 named modules, 58 worker
units. Final `metadata_errors: []`; 820 tests returned outcomes (647 pass,
155 fail, 18 error), and two modules timed out without complete test outcomes.
24 whole modules pass; 11 have service/fixture failures; 2 are incomplete.
Both source image modules, source transfer, installed/streamed/large/private/
observation backup, record-schema enumerations, refusal steps and frozen
reply-shape checks pass. Per-module results and seconds are in `evidence.json`.
Socket/service failures include denied collector binding, separate service
processes never reporting ready, and one worker launch fixture not completing;
no passing end-to-end claim is made for them.

`test_account_billing_admission` and `test_billing_gate` each hit the runner's
default 600-second worker timeout and were reaped. The whole compatibility
batch took 691.685 elapsed seconds / 1627.408 job-seconds, violating the
requested five-minute bound. A stop signal to the exact owned runner was
rejected by the sandbox. This execution mistake/limitation is recorded rather
than presented as green evidence. Future reruns must set
`COCOLO_WORKER_TIMEOUT=120` explicitly and use small named batches. No runner
or worker remains in flight at handoff.

The initial selection of `test_reply_shape_live` was refused because it is
soak-only; no tests ran in that preflight. Its wide nested service replay is
unrun here (socket restriction and five-minute scope); it remains a primary
verification gate on a suitable host.

Automatic idle/exhaustion replacement is deliberately deferred during pending
cleanup; no durable cross-process folder handoff is claimed. The explicit
stop/crash boundary remains the documented RS-B M2/M5 in-process retirement
boundary. A folder that remains undeletable retains its live custodian, slot,
byte reservations, memory holds and warning indefinitely while the service runs.

Only two existing assertions changed, as authorized by decision 309: the old
stop-after-twelve retry count is replaced by twenty due requests with exact
capped pauses; the old cleanup-stuck/replacement assertion is replaced by
continued custody and deferred automatic replacement. See [assertions.md](assertions.md).

Changed runtime files (under the native package's orchestration directory):
`cli.py`, `codex_agents/codex_preparation.py`, `codex_agents/project_driver.py`,
`codex_agents/service_host.py`, `codex_agents/history_api.py`,
`codex_agents/history_service.py`.
Changed tests/registration: the three requested test modules,
`packages/codex-agents/tests/lifetime_caps.json`,
`tests/test_setup_wizard_steps.py`, `scripts/test-requirements.json`, and new
`tests/requirements/prep-cleanup.json`. Terminal changes are in
`scripts/setup_wizard/flow.py` and `scripts/setup_wizard/steps.py`.
Documentation: `docs/requirements/prep-cleanup.md` plus this status,
`assertions.md` and `evidence.json`.

To repeat core verification from the checkout, with `PY` set to the test
interpreter supplied in the task:

```sh
COCOLO_WORKER_TIMEOUT=120 TMPDIR=/tmp/prepclean "$PY" -B scripts/parallel_tests.py \
  --module codex:tests.test_codex_preparation \
  --module codex:tests.test_run_driver_retries \
  --module codex:tests.test_memory_holds \
  --module codex:tests.test_lifetime_cap_guard \
  --module root:test_setup_wizard_steps \
  --module root:test_setup_wizard_source \
  --module root:test_setup_wizard_terminal
```

On a socket-capable host, repeat the blocked lifecycle explicitly:

```sh
COCOLO_WORKER_TIMEOUT=120 TMPDIR=/tmp/prepclean "$PY" -B scripts/parallel_tests.py \
  --module codex:tests.test_host_slots \
  --module codex:tests.test_project_driver
```

The other compatibility modules are named individually in `evidence.json`;
repeat only its failed/incomplete modules, in small named batches, with the same
worker timeout. No full suite, install, push or merge was run. No PTY-dependent
check was required for the terminal-list change; the terminal render unit tests
passed. Socket-backed end-to-end checks remain unverified here.

## Fix round (S2, after the primary's affected run)

The primary's `--affected` run reported three failures beyond the expected
digest pin. All three pass on the base (0.8.66).

1. `tests.test_memory_holds_crash` (M1 entry-delete and transition-kill): not a
   product interaction with the memory holds. The crash fixture starts a bare
   child (`python -m tests.memory_holds_fixture`) that imports
   `tests.test_codex_preparation`. This change added `from test_framework import
   requirements` at the top of that module, before the `tests.test_bridge`
   import that puts the scripts folder on `sys.path`, so the bare child died
   with `ModuleNotFoundError`. Fix: the import now follows the fixtures import
   (test-file import order only; no assertion changed, no existing test edited).
   The memory-hold rules (M1/M2/M5) and the product code were not touched.
2. `tests.test_b7b_budgets` (B3, 50.33 ms against the 50 ms limit): a load
   failure, not caused by this change. B3 times `release_earlier_unarmed_leases`
   in the account store, which this change does not touch. With machine load
   around 8 to 20, the branch passed 2 of 3 runs and failed 1 of 3; the base
   (0.8.66) failed 2 of 2 completed runs with the same B3 assertion.
3. `tests.test_host_commands` `OwnRootTests.test_every_allowlisted_command_runs_under_the_exact_prefix`:
   a temporary-directory cleanup race under load (`Directory not empty` while
   removing the test's home state folder), not caused by this change. Run alone
   three times each, it passed 3 of 3 on the branch and 3 of 3 on the base.

No product code changed in this round.

Fix-round verification (unsandboxed, 9 modules: memory_holds_crash, memory_holds,
memory_holds_lane, b7b_budgets, host_commands, codex_preparation,
run_driver_retries, host_slots, project_driver; 259 tests, 23 units, jobs 8, 154 s,
machine loaded): `failed_units: []`, `metadata_errors: []`. The slow-module
budget lines in that output (host_commands, project_driver and others) were
over their time budgets because of load and are not test failures.
