# RSF S4 validation

All test commands run from `packages/codex-agents`, one named unit module per
invocation, each under 300 seconds. No git commands, sockets, discovery or broad
suite were used. Existing tests changed only as approved in
[assertions.md](assertions.md), primary decision 164.

## Acceptance and results

| IDs | Tests in `tests.test_rsf_s4_slots` |
| --- | --- |
| RSF-S4-1, RSF-S4-6 | A real intent pruned before adoption stays held through sweep/reopen, with its historical launch fence. |
| RSF-S4-2, RSF-S4-6 | Validated stopped-instance v1 `claimed` journal releases immediately as `unsent`; repeated sweeps/reopen preserve its receipt. Live instances, spawning and unreadable instance locks provide no unsent proof. |
| RSF-S4-3, RSF-S4-6 | Retained validated run-bound deadline: held at deadline + grace −1 ms and equality, due at +1 ms, with the decision durable after restart. |
| RSF-S4-4, RSF-S4-6 | A validated watched native journal and positive alive observation beyond 24 hours stay held through adoption/sweep/reopen. Positive gone evidence releases without a completion or cleanup receipt. |
| RSF-S4-5 | Plain fixed reasons; no private values in reasons; evidence-only/mixed holds have null eligibility/finish time; status performs zero writes; renderer shows the owner command. |
| RSF-S4-8 | Detached owner signature, project/run scope, exactly one slot released, atomic rollback, restart/retry and nonexpiring signed confirmation. Direct collector dispatch checks old/wrong-runtime zero-write refusals, invalid record refusal, and a real signed success. |

Exact commands and measured unittest times:

```sh
python3 -B -m unittest tests.test_rsf_s4_slots
python3 -B -m unittest tests.test_legacy_capacity_upgrade
python3 -B -m unittest tests.test_active_run_capacity
python3 -B -m unittest tests.test_owner_record_payload
python3 -B -m unittest tests.test_run_holds
python3 -B -m unittest tests.test_rsf_s1_ingress
```

| Run | Result | Seconds |
| --- | --- | --- |
| Initial S4 behavioral red, before production edits | 7 tests; 6 failures, 1 missing-status error | 0.492 |
| Added collector/renderer checks before their code | 9 tests; 2 failures | 0.673 |
| Added proof retention/unreadable-lock checks before fixes | 12 tests; 2 failures | 0.902 |
| Added nonexpiring signed-record check before retention fix | 12 tests; 1 failure | 0.895 |
| Final S4 store module | 12 passed | 0.877 |
| Approved legacy-upgrade assertions | 12 passed | 0.857 |
| Active capacity, including existing two-clock protection | 16 passed | 7.809 |
| Existing owner signing payload bytes/isolation | 2 passed | 0.041 |
| Existing hold register, backups, restart and sweep regressions | 71 passed | 28.472 |
| Existing RS-F S1 ingress regressions | 11 passed | 0.528 |

Latest listed green runs total **124 test executions, 38.584 s** (including
inherited regression tests). No module approached the 300-second ceiling.

Preliminary fixture/import setup errors were corrected before the behavioral red
listed above. An intermediate active-capacity run failed its approved `:303`
assertion (7.574 s); the fixture was verified to retain a dispatch deadline, and
its approved retained-deadline branch was applied. That test now checks recorded
deadline + grace −1/equality/+1 rather than claim age. No other existing
expectation was changed. Existing fake-launcher helpers emit ResourceWarnings.
Touched Python sources and the unrun transport module were syntax parsed.

## New owner command and collector operation

Status supplies each deadline-free native Claude slot's exact command, with
shell-quoted registered project root, state root and run ID, ending in
`--signature owner-signature.bin`. The filename is the detached owner signature
for that slot. Running the command without `--signature` produces the record and
base64 signing payload for the existing owner signing workflow; it releases
nothing. The signed invocation and its retry release only that project's run.
The owner record and signature are retained without record expiry; the hold's
release evidence/receipt remains durable. Existing owner-record signing bytes
for other record types are unchanged.

B4m reply-shape table addition:

| Collector op | Request after envelope | Success body | Retry body |
| --- | --- | --- | --- |
| `confirm_legacy_slot` | `project_root`, `record` (closed `run_slot_confirmation`: `schema_version`, `record_type`, deterministic `record_id`, `run_id`), `signature_b64` | `{"ok":true,"committed":{"stored":true,"duplicate":false,"source_sequence":integer,"late":boolean}}` | `{"ok":true,"committed":{"stored":false,"duplicate":true,"source_sequence":same_integer}}` |

The envelope is `protocol`, `op`, `runtime_digest`. Operation/signature refusal
uses the existing `{"ok":false,"code":"operation_rejected"}` shape; incompatible
runtime uses the existing `runtime_upgrade_required` reply with its message and
update command. `owner_record_payload` also accepts the new record using the
existing `authenticated_owner_record` project-scoped signing purpose. The new
operation delegates to `commit_owner_record`; it grants no new caller authority.

RS-F S1 mutating-gate addition: **`confirm_legacy_slot`** is included in
`COLLECTOR_MUTATING_OPERATIONS`. `CollectorClient._request` stamps its runtime
identity and collector ingress checks it before lookup, signature verification
or writes. The direct S4 unit check covers missing/wrong runtime with unchanged
SQLite `total_changes`; no health preflight is required.

## Primary-only socket / CLI commands — not runnable here

New S4 transport tests are written in `tests.test_rsf_s4_transport`: actual
collector status and owner confirmation, forged/foreign signature and runtime
refusal, restart/retry, and the CLI's unsigned/sign/signed/retry flow. These tests
have not been executed in this no-sockets worktree.

```sh
python3 -B -m unittest tests.test_rsf_s4_transport
python3 -B -m unittest tests.test_holds_report_socket
python3 -B -m unittest tests.test_capacity_status_api
python3 -B -m unittest tests.test_lifecycle_cli_runtime
python3 -B -m unittest tests.test_learning_cli
python3 -B -m unittest tests.test_rsf_s1_ingress_socket
```

Run individually, each with a 300-second wall-clock ceiling, on the primary's
socket/CLI-capable harness. Existing CLI fixtures may also invoke git, which was
not authorized here. No transport acceptance is claimed from syntax parsing.
VERSION/runtime-releases.json release bookkeeping is left to the primary; the
new `legacy_slots.py` is included in the sorted protected-runtime file manifest.

## Changed files

- Requirements: `docs/requirements/rsf-s4.md`, `rsf-s4/assertions.md`, this file;
  B4m and S1 validation cross-references.
- Runtime under `native-package/.codex/orchestration/`: `cli.py`;
  `codex_agents/active_run_capacity.py`, `legacy_slots.py`, `learning_store.py`,
  `learning_contracts.py`, `owner_record_payload.py`, `collector_client.py`,
  `collector_contracts.py`, `client_runtime.py`, `run_holds_schema.py`, `runtime_files.py`.
- New tests: `test_rsf_s4_slots.py`, `test_rsf_s4_transport.py`.
- Approved existing test changes: `test_legacy_capacity_upgrade.py`,
  `test_active_run_capacity.py` only.
