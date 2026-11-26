# Batch 0.8.69 pre-merge (install point IP-C)

Branch `merge/batch-0869`, worktree `cocolo-batch12`, base e46759a5 (accepted 0.8.68). `version/0.4.0` not moved. No VERSION
bump, no digest change, no push. `test_reply_shape_live` NOT run (primary, quiet machine).

## Merges (`--no-ff`, in order)

| # | Branch | Tip | Decisions | Merge commit | Conflicts |
|---|--------|-----|-----------|--------------|-----------|
| 1 | feat/s13b-due-drain (with S13 a09e71f0, S13a d1af5e86) | 94a3eecb | 322, 354, 356, 359 | 8d1ebba6 | test-requirements |
| 2 | feat/s10-resources | d5052285 | 355, 360, 361 | e22b0274 | learning_store.py, test_health_containers.py, test-layers, test-requirements, test-stages, test-timings |

## Conflicts and resolutions

- Registry JSON (test-layers, test-stages, test-timings, test-requirements): both sides appended entries at the end of the same
  map. Resolved as unions (ours then theirs, comma-joined); nothing dropped (checked that every key of both parents is
  present and unchanged; all registries parse with `json.tool`).
- `learning_store.py` (S10 vs S8/S11 wiring): imports unified to `from . import health_resources, health_workspaces, memory_holds`;
  the collector registers holds (when a ledger owner exists), workspaces, then `health_resources.register(...)`. All checks kept.
- `test_health_containers.py`: expected check names are now `['self_check','containers','workspaces','resources']` and the
  state map gains `'resources': 'ok'` (no ledger owner here, so no holds).
- Non-textual consequence of the union: `test_health_monitor.py` line 413 expected
  `['self_check','containers','workspaces']` -> now `[..., 'workspaces','resources']` (merge union, not a product change).
  `test_health_holds` uses a subset check on `holds`, unchanged.
- `lifetime_caps.json`: auto-merged, but S13a's new `min(limit,64)` in `project_history.py::release_earlier_unarmed_leases`
  was never listed, so `test_lifetime_cap_guard` failed (it was not in S13a/S13b's run lists). Added an entry (BOUNDED: one
  selection per call, rows leave the due set by release or backoff; test
  `test_full_blocked_pass_reaches_eligible_rows_then_waits_for_backoff`). Registry data, no product change.
- Op sets, reason vocabularies (`resource_low`), `run.check` in the driver ops, runtime_files: auto-merged, nothing lost.

## Modules run (named, `--jobs 2`, TMPDIR=/tmp/claude-501/batch12; all `metadata_errors []`, `failed_units []`)

- After S13b: test_s13b_due_drain, test_s13_budgets, test_s13a_lease_pass, test_result_leases, test_project_history,
  test_result_lease_lane, test_project_driver (269-287 s, over its 60 s budget under 2 jobs; the budget debt is not new),
  test_b7b_budgets: green.
- After S10: test_health_resources, test_health_resources_open, test_health_containers, test_health_monitor, test_health_holds,
  test_health_workspaces, test_lifetime_cap_guard, test_account_original_open, test_dispatch_client_runtime, test_refusal_steps,
  test_contracts, test_project_driver, test_run_driver_retries, test_history_api, test_reply_shape_compatibility,
  test_dispatch_delivery_cli, test_project_host_open, test_host_slots, test_learning_cli (65 s): green after the two fixes above.

## Carried items

a. S13b LOW (`result_persistence.py`, expire path). Test first: `test_wrapper_failure_before_the_body_leaves_no_stale_continuation`
   (S13B-IDLE, registered in test-requirements). Red: calls `[64, 64, 64]` instead of `[64]` (a stale True requeued a failed
   request). Fix: `history._unarmed_lease_pass_pending = False` before the call, flag read after it; a body that fails late keeps
   what it set. Comments added on the live-lease deferral case (it backs the row off like any blocked row and sets the flag by
   design) in both files. Stray third blank line before `S13RegressionControls` in `test_s13_budgets.py` removed. Green.
b. S10 `test_account_original_open.test_M11_refused_registration_retries_...`: old -> new, assertions unchanged.
   Old: `mock.patch.object(client_runtime, '_check_new_run')` (guard bypassed) and `_run_driver` side_effect list.
   New: the real `_check_new_run` runs (its `run.sock` guard is satisfied by a touched file in a temp dir that replaces the
   hard-coded `/private/tmp/m11` endpoints); `run.check` goes through the real `_run_driver` and its reply validation, with
   `client_runtime.exchange_frame` replaced by a protocol-valid ready reply `{'protocol':1,'state':'ready','reason_code':None}`
   (it also asserts the request is `run.check`); registration calls still go to the `register` mock, so
   `register.call_count == 2` is unchanged. Green.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
