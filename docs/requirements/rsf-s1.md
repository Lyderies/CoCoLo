# RS-F S1: runtime identity at mutating ingress

Source: `~/Code/temp/orchestrator-child-liveness/ANALYSIS-0.4.0-rsf-migration.md`,
section 2, Old CLI row, and section 4, S1; owner decision 129.3.
Requested branch/base: `feat/rsf-s1-ingress`, `8d4a7ce3` (no git invoked).

**RSF-S1-1.** Every mutating collector/account request must carry the caller's
`runtime_digest`, checked against the serving process's loaded runtime identity
before any database, filesystem, binding-token or lifecycle mutation. Missing,
malformed or different identity refuses with `runtime_upgrade_required`, with
zero writes, a plain message and the exact command `./setup.sh --update`.
Clients must surface that message and command. A prior health call is neither
required nor sufficient. Matching callers retain the existing operation checks.
There is no pre-1.0 compatibility for older clients/runtimes. Store migrations,
live predecessor revocation and runtime epochs are later slices.

No existing shared compatibility-message helper was found. The implementation
shares a fixed message and command from `collector_contracts.py:18` (the command
already appears in `docs/setup-wizard.md:31`).

**RSF-S1-2.** Inventory every collector/account mutating wire operation, including
memory-only lifecycle operations, and identify its pre-write runtime check.
Paths below are relative to
`packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.
References below describe the resulting implementation. Collector mutations
share `learning_store.py:12943` (before all operation-specific branches).
Account mutations share `AccountService.runtime_upgrade_reply` at
`account_service.py:977`, called before decoding at `account_service.py:1002`
and before the socket front decoder at `account_ingress.py:220`.

| Collector operation | Dispatch site | Covering check |
| --- | --- | --- |
| `bind` | `learning_store.py:12950` | collector dispatch runtime gate, before binding expiry pruning / `bind_session` |
| `ingest` | `learning_store.py:13006` | same gate, before consuming binding token / append |
| `issue_result_repair` | `learning_store.py:13035` | same gate, before project lookup / receipt expiry deletion / insert |
| `register` | `learning_store.py:13049` | same gate, before project registration |
| `commit_owner_record` | `learning_store.py:13055` | same gate, before signature work / record commit |
| `learning_moved_if_empty` | `learning_store.py:13154` | same gate, before migration marker write |
| `shutdown_if_drained` | `learning_store.py:13159` | same gate, before stop event |
| `shutdown` | `learning_store.py:13166` | same gate, before stop event |

Account retains its existing central authorization check at
`account_service.py:1041`, before authorizer, jobs or owner views. S1's earlier
gate supplies the specified upgrade refusal for missing/malformed/different
mutating caller identities, including envelopes the operation decoder rejects.

| Account operation | Dispatch site | Covering check |
| --- | --- | --- |
| `account.billing_probe` | `account_service.py:1077` | account mutating runtime gate before decode/authorization/jobs |
| `account.observe_provider` | `account_service.py:1104` | same gate |
| `account.record_observation_unavailable` | `account_service.py:1134` | same gate |
| `account.open_host_run` | `account_service.py:1148` | same gate |
| `account.claim_launch` | `account_service.py:1192` | same gate |
| `account.register_execution` | `account_service.py:1211` | same gate |
| `account.run_provenance` | `account_service.py:1218` | same gate (conservatively included with all non-read operations) |
| `account.open_provenance` | `account_service.py:1218` | same gate (conservatively included with all non-read operations) |
| `account.release_capacity` | `account_service.py:1246` | same gate |
| `account.submit_terminal` | `account_service.py:1280` | same gate |
| `account.record_rate_limit_event` | `account_service.py:1289` | same gate |
| `account.close_denied_attempt` | `account_service.py:1294` | same gate |
| `account.fence_claim` | `account_service.py:1300` | same gate |
| `account.settle_lost_run` | `account_service.py:1305` | same gate |
| `account.withdraw_open` | `account_service.py:1311` | same gate |

Read-only collector operations: `health`, `project`, `learning_migration`,
`owner_record_payload`, `verify_owner_record`. The latter two read the checked
owner key; they do not install keys or persist records. Account read operations
are the six members of `ACCOUNT_READ_OPERATIONS` (`account_api.py:54`): health,
project, owner_key, owner_public_key, capacity_status, provider_state_snapshot.
Internal sweeps and direct offline owner maintenance are not wire requests.

Inventory: **23** mutating operations (8 collector + 15 account).
At base, **8** lacked a serving-runtime check; account's existing 15 checks lacked
the specified refusal contract. In the implementation, **0 unchecked** mutating
operations remain. Current collector clients attach identity at
`collector_client.py:173`; collector refusals surface at `collector_client.py:198`;
account refusal decoding/message are at `account_api.py:393` and `account_api.py:93`.
Tests and run instructions: [validation](rsf-s1/validation.md).
Work stopped at a required existing exact-frame assertion change, recorded
old -> new in [assertions](rsf-s1/assertions.md); no existing test file was edited.
