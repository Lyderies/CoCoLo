# U1 status — Unit 3.9 slice 5

Base `afb6f610` (0.8.66), branch `feat/u1-orch-lifetime`. Product implementation
and 16 new tests are complete in the worktree. No commit (sandboxed); no install,
merge, VERSION bump, or digest-catalogue edit. The external runner remains
read-only. Its required automatic calls and their exact insertion points are
specified in [u1.md](../u1.md). Automatic provider process replacement is the
runner's responsibility; this unit tests the product side and its entry point.

## Requirement → test → evidence

All names below are in `tests.test_u1_orchestration_lifetime.OrchestrationLifetimeTests`.
Every requirement's initial boundary tests were observed red before product
implementation. Initial red: 10 tests, missing rollover/packet protocol and state
support; `metadata_errors: []`. Later additions observed red before each fix:
enrolled runner entry point, explicit durable identity validation, and unchanged
reply sequence/state validation. Final module: **16 tests PASS**, about 1.04 s
including worker startup; every U1 registry check is linked, metadata errors empty.
Sanitized per-test outcomes and timings: [evidence.json](evidence.json).
No raw logs, machine home paths or temporary evidence dependencies are saved here.

| Requirement | Executable test names | Result |
|---|---|---|
| O0C-U1-1 | `test_registration_boundary_rolls_at_real_512_and_continues_past_it` | Red → green; 512 retained registrations report fully, the 513th belongs to the successor. Token arithmetic and its limits are in u1.md. |
| O0C-U1-2 | `test_process_exit_between_close_and_open_rolls_back_then_replays`; `test_rollbacks_and_lost_reply_never_leave_zero_or_two_open`; `test_exact_enrolled_runner_call_uses_one_exchange_and_reconciles_lost_reply`; `test_revocation_and_new_identity_cannot_fork_a_closed_predecessor`; `test_runner_requires_durable_explicit_identity_before_enrollment` | Red → green; real abrupt process exit before open, rollback after both ends, lost reply/reopen, revocation, fork prevention, exactly one enrolled wire exchange per invocation. |
| O0C-U1-3 | `test_independent_registration_report_open_and_mixed_boundaries`; `test_overlap_does_not_double_charge_and_attempt_rows_do_count`; `test_repeated_registration_rollovers_past_smaller_independent_registration_cap`; `test_events_replay_and_reserved_close_roll_repeatedly_past_limit`; `test_pending_work_returns_only_internal_wait_and_retries_after_drain`; `test_unchanged_capacity_reply_cannot_substitute_a_new_sequence_or_closed_state` | Green; independent patched registration/report caps; overlapping rows and attempt counts; repeated past-cap operation; five event rollovers with exact replay and reserved close at event three; pending work keeps one open and resumes automatically after drain. Original event boundary test was red before implementation. |
| O0C-U1-4 | `test_handoff_links_packet_and_state_survive_restart_without_file`; `test_restart_without_handoff_generates_packet_and_rejects_corrupt_state`; `test_resume_state_exact_byte_limit_and_one_past_preserves_packet` | Red → green; state survives ordinary checkpoints and restart, generated packet needs no file, sealed corruption refused, exact 2,048 bytes accepted and one past refused without changing state. |
| O0C-U1-5 | All 16 tests, including `test_wire_rejects_changed_link_wrong_scope_and_false_rollover_receipt`; existing `tests.test_project_orchestration` | Green; request replay, independent boundaries, complete reports, wire integrity and crash/restart coverage. No pre-existing assertion changed or removed; see assertions.md. |

## Named test runs

Final focused run: `test_u1_orchestration_lifetime` (16),
`test_project_orchestration` (38), `test_lifetime_cap_guard` (4), and
`test_phase_report` (14): **72/72 pass**, slowest worker about 1.21 s.
`test_runtime_release`: 2/3 pass; only
`test_current_runtime_is_recorded_for_the_current_version` fails, expected
because the requested VERSION/catalogue freeze preserves the old digest.
The runtime file-set assertion passes: the new module is included in the
protected-copy file manifest. Final focused run metadata errors: **[]**.

Record-shape compatibility run (optional sealed checkpoint fields; no table,
column, schema-version, account/collector operation or reason-code changes):
**366 tests, 344 pass, 18 fail, 4 error (per-test outcomes); 24.83 s worker wall time; metadata []**.
All requested named workers completed; none timed out. Full module outcomes:

| Named module (`codex:tests.` prefix) | Tests | Result |
|---|---:|---|
| test_account_observation_backup | 2 | PASS |
| test_backup_installed_project | 2 | PASS |
| test_backup_reply_private | 11 | PASS |
| test_large_project_backups | 8 | PASS |
| test_owner_backup_streamed | 40 | PASS |
| test_learning_source_image | 33 | PASS |
| test_learning_source_image_legacy | 7 | PASS |
| test_learning_source_transfer | 13 | PASS |
| test_history_service_upgrade | 30 | PASS |
| test_project_holds_schema | 40 | PASS |
| test_project_history | 35 | PASS |
| test_refusal_steps | 14 | PASS |
| test_reply_shape_compatibility | 3 | PASS |
| test_b6_unattended_close | 41 | PASS |
| test_history_api | 31 | 27 pass; 4 process-server startup failures |
| test_service_host | 17 | 4 pass; 10 fail; 3 error at Unix listeners/service startup |
| test_state_location | 22 | 19 pass; 2 fail; 1 error at collector startup |
| test_learning_migration | 17 | 15 pass; 2 CLI failures; both reproduced on unchanged base |

A direct short-path AF_UNIX bind returned `PermissionError`, errno 1,
`Operation not permitted`. Thus the real transport/collector scenarios cannot
be certified here. The two migration failures were also reproduced in an
isolated temporary package copy using the unchanged base runtime (17 tests,
22.161 s): `test_command_moves_reports_refuses_and_rolls_back` fails at the
post-migration service cycle; `test_first_start_of_the_project_service_moves_the_history_by_itself`
fails starting its orchestration. No migration assertions were changed.

Not run: `test_reply_shape_live`'s nested soak replay. Its initial named
invocation failed preflight because it requires `--profile soak`, before any
worker ran. The long replay includes the already blocked socket/service paths;
it was not launched with a longer profile. The ordinary reply-shape enumeration
and compatibility module passed. No full suite, provider/PTY session, external
runner test or live hand-off campaign was launched. No tests remain in flight.

Primary rerun (use the supplied test interpreter as `$PY`, from repo root):

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_u1_orchestration_lifetime --module codex:tests.test_project_orchestration --module codex:tests.test_lifetime_cap_guard --module codex:tests.test_phase_report
$PY -B scripts/parallel_tests.py --module codex:tests.test_history_api --module codex:tests.test_service_host --module codex:tests.test_state_location --module codex:tests.test_learning_migration
$PY -B scripts/parallel_tests.py --module codex:tests.test_reply_shape_live --profile soak
```

The last command is for an unrestricted primary session with the normal soak
budget; it is not part of this unit's fast test campaign. Release digest
verification becomes green only after the primary's release bookkeeping.

## Changed files

- Runtime: `client_runtime.py`, `orchestration_api.py`, `orchestration_store.py`,
  new `orchestration_lifetime.py`, and `runtime_files.py` under
  `packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.
- Tests: new `packages/codex-agents/tests/test_u1_orchestration_lifetime.py`;
  `packages/codex-agents/tests/lifetime_caps.json` registers the new state byte
  limit and continuation checks, and describes the existing cap lifetimes.
- Metadata: `scripts/test-layers.json`, `scripts/test-stages.json`,
  `scripts/test-timings.json`, `scripts/test-requirements.json`,
  new `tests/requirements/u1.json` (new process-starting module is scenario/integration).
- Requirements/evidence: `docs/requirements/u1.md`, this file,
  `docs/requirements/u1/assertions.md`, `docs/requirements/u1/evidence.json`.

Residual integration work outside the authorized worktree: the runner must
adopt the documented enrolled call before `end_session` and before new work;
serialize admission; checkpoint structured state as it changes; reconcile a
lost hand-off reply before launching the planned next provider session; read
its resume packet from the product. This product library cannot call or patch
the external runner. There is no user hand-off/refusal step in this contract.
