# B5a validation

Requirements: [../b5a.md](../b5a.md). Runtime paths below are relative to
`packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.
Original B5a delivery edited no existing test file or assertion. This review
adds regressions and stops the never-spawned fixture before closure; no existing
assertion was edited. No new account operation, schema
step, runtime file, count limit, reply field or op-set pin was introduced.

| Acceptance | Implementation | New coverage |
| --- | --- | --- |
| RS-B-B5a-1 | `launch_journal.py`: `process_gone`, `tree_state`, checked `gone` state; existing watchdog marker and kernel scans | `test_crashed_process_evidence`: live/missing confirmation, both scans required, unknown/failed write, never spawned; real watchdog check available on primary |
| RS-B-B5a-2 | `account_lost_run.py`: `lost_run_closure`; `lost_run_closure.py`: existing field set, added kind | `test_crashed_account`: real SQLite claim/release/closure, no-proof refusal, matching/repeatable crashed closure, mismatched lease, settled never-spawned admission; production wire decoder and B4m field guard |
| RS-B-B5a-3 | `launch_journal.py`: protected, bounded `process_settlement_evidence`; `project_history.py`: `open_account_settlement`, `record_account_settlement` | `test_crashed_settlement`: V3, V7, wrong identity/request/key, unreadable/container proof, bound reservation cannot use unbound exception, A5/L6b |
| RS-B-B5a-4 | `project_driver.py`: `_settle_entry`, `_settle_intent`; B4s writer/outbox and existing persistence lane | `test_crashed_driver_unit`: proof → account → writer → workspace → journal order; stop before record/after commit; failed proof; durable fence admission and stopped-instance retry. Store retries after restart/prune/purge |
| RS-B-B5a-5 | existing B4s transaction/custody/readers, now accepting verified crashed closures | `test_crashed_settlement`: X4 committed/uncommitted, oracle S3 counts, verified reader, validator, tombstone, admitted-unbound lease `lost_unbound` |
| RS-B-B5a-6 | `client_runtime.py`: `_lost_run`, existing output keys | `test_crashed_driver_unit.CrashedStatusTests`; existing reply-field compatibility module |

## Red before implementation

From `packages/codex-agents`:

```sh
python3 -B -m unittest tests.test_crashed_settlement
python3 -B -m unittest tests.test_crashed_process_evidence tests.test_crashed_account
```

`tests.test_crashed_settlement`: 9 tests, 1 pass, 8 errors, 0 failures, 0.080 s, before
production edits. The base rejects the crashed closure kind and has no durable
`process_gone` transition. The ordinary lost-unbound control already passed.
`tests.test_crashed_process_evidence` + `tests.test_crashed_account`:
7 tests, 0 pass, 7 errors and one real-watchdog environment failure (including
fixture cleanup errors), 6.651 s, also before production edits.

Additional red regressions preceded their fixes:

- `tests.test_crashed_settlement`: 12 tests, 11 pass, 1 failure, 0.133 s;
  the durable never-spawned admission still left lease_case `armed` instead
  of `admitted_unbound`. The fix uses the same outbox transaction and releases
  the lease `lost_unbound` through B4s, even with a `crashed` account closure.
- `tests.test_crashed_driver_unit`: 7 tests, 6 pass, 1 failure,
  0.067 s; recovery wrongly assigned the admission to the current service,
  hiding it from the next sweep after a failure. The claimed journal entry
  now retains the stopped intent's instance.

New fixture setup was repaired before the recorded red runs (`release`, not a
nonexistent journal `close`; the existing account owner view, not a nonexistent
ledger closure method). Existing assertions were untouched.

## Final local units

Run each named module separately from `packages/codex-agents`:

```sh
python3 -B -m unittest tests.test_crashed_settlement
python3 -B -m unittest tests.test_crashed_process_evidence
python3 -B -m unittest tests.test_crashed_account
python3 -B -m unittest tests.test_crashed_driver_unit
python3 -B -m unittest tests.test_account_settlement
python3 -B -m unittest tests.test_account_settlement_wire
python3 -B -m unittest tests.test_reply_shape_compatibility
python3 -B -m unittest tests.test_publication_replay
python3 -B -m unittest tests.test_lifetime_cap_guard
```

Final execution loaded those same named unittest modules, each in a separate
Python process with a 290-second timeout, rejecting git subprocesses and socket
creation. All guards passed. Historical aggregate results (raw logs removed):

| Named test module | Pass | Fail | Skip | Test seconds | Wall seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| `tests.test_crashed_settlement` | 13 | 0 | 0 | 0.142 | 0.652 |
| `tests.test_crashed_process_evidence` | 4 | 0 | 1 | 0.647 | 0.777 |
| `tests.test_crashed_account` | 4 | 0 | 0 | 1.856 | 2.337 |
| `tests.test_crashed_driver_unit` | 7 | 0 | 0 | 0.063 | 0.575 |
| `tests.test_account_settlement` | 33 | 0 | 0 | 0.265 | 0.774 |
| `tests.test_account_settlement_wire` | 6 | 0 | 0 | 0.002 | 0.490 |
| `tests.test_reply_shape_compatibility` | 3 | 0 | 0 | 0.003 | 0.123 |
| `tests.test_publication_replay` | 40 | 0 | 0 | 0.641 | 1.169 |
| `tests.test_lifetime_cap_guard` | 4 | 0 | 0 | 0.673 | 0.713 |

**115 tests: 114 pass, one environment skip; every module under 3 seconds wall.**
The skip is the real-watchdog test: this sandbox denies `/bin/ps` (`operation not
permitted`), so `process_tree.process_table()` returns None and its watchdog
cannot acknowledge. The controlled-kernel tests still require both scans to
succeed and verify refusal of alive/unknown/missing-confirmation evidence.
The real-kernel check must run on the primary; it must not remain skipped there.

An earlier `python3 -B -m unittest tests.test_deadlines_retention` also passed
36 tests in 7.110 s. This was before the final admitted-unbound additions; it is
supplemental evidence, not part of the guarded final total.
All five new test modules, including the unexecuted socket module, were parsed.
`python3 -B -m ruff check ...` could not run because ruff is not installed.

## Constraint deviation caught during validation

The first reused account fixture indirectly invoked git to initialize its
**disposable temporary test repositories**, before that dependency was noticed.
The supplemental retention module also uses that fixture. No git command was
run against this worktree. The new account fixture now replaces only that git
setup helper (account metadata does not require a repository); the final runs
actively reject git and socket creation. No sockets were opened in local checks.
No writes were made to another source worktree. Temporary test state used the
sandbox's permitted temporary directories.

## Primary-only process, socket and CLI checks — not executed here

Run these exact commands from `packages/codex-agents` in a primary environment
with process-table access, Unix sockets and disposable git fixtures permitted:

```sh
python3 -B -m unittest tests.test_crashed_process_evidence
python3 -B -m unittest tests.test_crashed_settlement_socket
python3 -B -m unittest tests.test_account_settlement_driver.StepBoundaryTests
python3 -B -m unittest tests.test_account_settlement_recovery.ReviewRoundOneDriverTests.test_r1_M1_a_slot_released_as_crashed_has_no_released_unacknowledged_closure
python3 -B -m unittest tests.test_rsf_s1_ingress_socket
python3 -B -m unittest tests.test_holds_report_socket
```

`test_crashed_settlement_socket` is a new, syntax-checked primary-only test. It
uses the existing real killed-host harness, records an account crash while the
child is alive (closure still refused), kills the host, lets process proof drive
the real account socket/closure/writer path, and checks `learning-run-status`
for unknown outcome, released capacity and plain wording. The B4s live-child
assertion remains unchanged. StepBoundaryTests retains B4s's real writer-lane
ordering/replay checks. The S1 module checks the existing mutating ingress gate;
the holds-report module includes its isolated terminal `learning-status` check.
These commands are listed, not claimed green. B5b/container validation is deferred.

VERSION/runtime-releases.json remain unchanged for primary release bookkeeping;
the current-runtime digest pin is consequently not claimed green. No new runtime
file was added, and B4m's exact reply catalogue and lifetime-cap guard passed.

## Review LOW fixes (decision 216)

Each new regression failed before its corresponding production/documentation fix.
All commands ran from `packages/codex-agents` with `python3 -B -m unittest`
and a named module. No existing assertion was edited.

| Named test module / new regression | Red pass / fail | Red seconds | Green pass / fail | Green seconds |
| --- | --- | ---: | --- | ---: |
| `tests.test_crashed_settlement` / `test_stored_crash_proof_has_only_public_process_fields` | 13 / 1 | 0.158 | 14 / 0 | 0.149 |
| `tests.test_crashed_account` / `test_live_instance_never_spawned_entry_cannot_authorize_crashed_closure` | 4 / 1 | 2.013 | 5 / 0 | 2.248 |
| `tests.test_b5a_validation_privacy` / `test_validation_has_no_raw_log_links_or_files` | 0 / 1 | 0.001 | 1 / 0 | 0.000 |

The saved hold evidence contains only state, child/watchdog PID and birth, and
run-marker digest (null for a never-spawned admission). Account unbound proof
requires a claim_pending lease and a never-spawned entry; a claimed entry also
requires positive stopped-instance lock evidence. A settled tombstone is final.
The never-spawned closure fixture now stops its owner before asking for closure.

Additional review checks: `tests.test_crashed_driver_unit` 7 pass, 0 fail,
0.075 s; `tests.test_account_settlement` 33 pass, 0 fail, 0.290 s;
`tests.test_account_settlement_wire` 6 pass, 0 fail, 0.002 s;
`tests.test_crashed_process_evidence` 4 pass, 0 fail, 1 environment skip,
0.659 s (same denied process-table access described above).
No git commands or sockets were used in this review round. Raw red/green log
files were deleted; only aggregate results are retained here.
