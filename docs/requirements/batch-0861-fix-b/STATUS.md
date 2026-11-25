# Batch 0.8.61 integration fix B

Accepted scope: RS-A G6 keeps empty maintenance read-only and the frequent idle
hold check to one indexed read; P9-2/P9-3 retain bounded inspection/deletion and
exact owner-generation validation when there is work. The signature pool child
fixtures must import the shared capture fixture with its P9 test metadata intact.
No existing assertion is changed or removed; no new requirement IDs or modules.

Requirements -> existing tests (run red before implementation):

- RS-A G6 -> `tests.test_ledger_lock_limits.LedgerLockLimitTests.test_RS_A_G6_an_idle_ledger_with_nothing_due_is_checked_often_by_one_cheap_read`:
  reproduced 20 subtest failures (owner history scans and empty BEGIN/COMMIT).
- Pool subprocess fixtures -> `tests.test_signature_worker_pool`: reproduced
  `ModuleNotFoundError: No module named 'test_framework'` in both children, also
  reproduced directly with the isolated faults fixture before editing it.
- P9-2/P9-3 -> `tests.test_signature_inputs`,
  `tests.test_p9b_attestation_followups`; RS-F S2 -> `tests.test_rsf_s2_fence`;
  ledger compatibility -> `tests.test_learning_store`: results below.

The named-module red run also encounters the environment's Unix socket bind
restriction (`PermissionError: Operation not permitted`). Record this separately
from the integration failures. No schema/record shape, VERSION, or digest catalogue
change is authorized; any runtime edit requires the primary to re-record the digest.

## Causes and fixes

1. `attestation_capture.py` at original lines 153 and 201 read owner generation
   before discovering empty inspection work and again before an unconditional
   `_account_transaction()` at original line 203. RS-F S2 makes that transaction
   visible as BEGIN IMMEDIATE/COMMIT. This happens during full maintenance cycles;
   the frequent `_holds_due_idle()` path itself still executes only
   `account_holds_due()`'s one indexed read. Current lines 153-165 inspect first
   and return without owner reads or a transaction when both inspection and
   deletion work are empty. Lines 210-214 also skip the transaction when there
   are no new verdicts or superseded rows. Independent bounded cursors, exact
   rechecks, verifier retry behavior and nonempty deletion work are preserved.
2. `signature_pool_faults.py:18` and `signature_pool_blocked.py:21` established
   package/runtime paths but omitted the repository scripts directory needed by
   `test_signature_inputs.py:17` since P9. Add scripts to those existing child
   path setups (inside main). No helper extraction, decorator changes, assertion
   changes, new import-time I/O, or runtime worker changes.

## Evidence and remaining verification

Both runs completed; none remain in flight. Durable per-test outcomes and runner
metadata are in [evidence.json](evidence.json). Red wall time: 77.6 s; after-fix
wall time: 91.5 s. Both report `metadata_errors: []`. Counts below count methods,
not multiple subtest/teardown failures for the same method.

| Requirement / boundary | Named module and result after fix | Worker time |
| --- | --- | --- |
| RS-A G6 | `tests.test_ledger_lock_limits`: G6 idle-read test **red -> green**, 26/27 methods pass; only health-publication socket test errors | 10.086 + 16.273 s |
| Pool fixture imports | `tests.test_signature_worker_pool`: import errors eliminated; `test_blocked_fixture_failure_waits_for_all_actual_roots_before_cleanup` **red -> green**; 7/14 methods pass | 90.824 s |
| P9-2/P9-3/P9-4 | `tests.test_signature_inputs`: 43/44 pass, including every projection/cursor/retry/VM-budget test; P9-1 deferred claim times out and fails worker cleanup | 41.185 s |
| P9b-1/P9b-2 | `tests.test_p9b_attestation_followups`: **15/15 pass** | 5.029 s |
| RS-F S2 | `tests.test_rsf_s2_fence`: **12/12 pass** | 3.315 s |
| Ledger compatibility | `tests.test_learning_store`: **95/100 pass**; five socket-dependent methods error at listener.bind | 10.097 + 8.327 s |

This restricted environment denies Unix socket bind. Independent probes also
confirm `/usr/bin/sandbox-exec` fails with `sandbox_apply: Operation not permitted`
and `/bin/ps` is denied. Pool failures proceed past imports into actual worker
completion, metadata and cleanup checks; full pool and deferred-claim green
cannot be claimed here. Pool timing exceeds the 60 s budget while these worker
checks time out. No assertions or budgets were weakened. No pty-specific work was
needed. All six requested modules were attempted; an unrestricted rerun remains
necessary for complete green. No full suite or unrelated modules were run.

From the worktree root, rerun with the supplied test venv interpreter as `$PY`:

```sh
$PY -B scripts/parallel_tests.py --module codex:tests.test_ledger_lock_limits --module codex:tests.test_signature_worker_pool
$PY -B scripts/parallel_tests.py --module codex:tests.test_signature_inputs --module codex:tests.test_p9b_attestation_followups --module codex:tests.test_rsf_s2_fence --module codex:tests.test_learning_store
```

Only `attestation_capture.py`, the two pool fixture scripts, and this status /
evidence directory were edited for this task. Other worktree edits belong to the
concurrent builder. `git diff --check` passes for these files. No schema or record
shape change; no copier expansion is required. No new modules or requirement IDs
need registry changes. No VERSION or digest catalogue edits. **The runtime digest
will change because attestation_capture.py changed; the primary must re-record
it.** `tests.test_runtime_release` was not run (the unrecorded digest would be
expected to fail). Changes remain uncommitted as requested.
