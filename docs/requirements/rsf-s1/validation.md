# RSF S1 validation

All commands run from `packages/codex-agents`, one named module at a time, each
with a five-minute ceiling. Do not run discovery or a full suite.

## Worktree (no sockets, no git)

```sh
python3 -B -m unittest tests.test_rsf_s1_ingress
```

The old-format repair test calls the real collector dispatch directly, without
health or a listener. It seeds an expired receipt and checks SQLite total_changes
and a complete logical database snapshot, catching deletion as well as insertion.
The module also covers mismatched/malformed digests, all 23 inventoried operation
gates, binding-token preservation, current clients and surfaced refusal messages.

## Primary runs (sockets required; not runnable in this worktree)

```sh
python3 -B -m unittest tests.test_rsf_s1_ingress_socket
python3 -B -m unittest tests.test_collector_read_scope
python3 -B -m unittest tests.test_collector_stale
python3 -B -m unittest tests.test_service_transport
python3 -B -m unittest tests.test_account_api
python3 -B -m unittest tests.test_account_ingress
python3 -B -m unittest tests.test_legacy_capacity_upgrade
```

The new socket module sends old-format repair and all mutating collector
operations directly over AF_UNIX, and proves current-client repair success and
plain update instructions for a mismatched client. Existing modules are regression
runs; no existing test assertion has been edited. If an existing assertion needs
to change, stop and record old -> new in `assertions.md` before proceeding.

## Results

- Before implementation: `python3 -B -m unittest tests.test_rsf_s1_ingress`
  ran 10 tests in 0.457 s, exit 1 (79 failing subtests, 2 errors). The direct
  old-format repair regression failed at the zero-write assertion: SQLite
  `total_changes` was **5 -> 7** (expired receipt deletion plus new receipt).
- After the handler/client fix: the same module passed all 10 tests in 0.455 s.
- Account front-thread coverage added before its fix: the same module ran 11
  tests in 0.466 s, exit 1 (29 failing subtests, 1 error). Missing identities
  were dropped; a valid wrong-runtime envelope went toward owner handoff.
- After sharing the side-effect-free gate with the account front thread:
  `python3 -B -m unittest tests.test_rsf_s1_ingress` passed **11 tests in 0.476 s**.
- No sockets or git were used. Existing test modules were not executed.
  The four new socket tests are written, but **not runnable here**; primary
  commands above are exact. The new account socket test also checks all 15
  operations without owner pumping, authorization or database changes.
- **Stopped at an existing assertion conflict**, as requested. Exact proposed
  old -> new is in [assertions.md](assertions.md). No existing assertion or
  existing test file was edited. Primary regression runs remain pending; the
  unchanged exact-frame assertion in `test_service_transport` is known to conflict.

## RSF S4 mutating-gate addition

`confirm_legacy_slot` is now in `COLLECTOR_MUTATING_OPERATIONS`; client stamping
and the pre-write runtime gate therefore apply. Its missing/wrong-runtime
zero-write test is in `tests.test_rsf_s4_slots`. Exact wire shapes and the
primary-only socket/CLI command are in
[RSF S4 validation](../rsf-s4/validation.md#new-owner-command-and-collector-operation).
