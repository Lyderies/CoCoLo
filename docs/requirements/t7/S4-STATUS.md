# T7 slice S4: updates from earlier releases (L12.1, L12.6, L12.7, G7)

Module `scripts/e2e_checks_update_history.py`, unit tests `tests/test_e2e_update_history.py`. Branch feat/t7-s4 (base cbc757d5, 0.8.60).
Run: `scripts/e2e_lifecycle.py --execute <check names>` (one process per scene; `--flows` runs every G check, do not use).

## How an earlier release is installed
`git archive <tag>` into the world, that tag's `scripts/install.py install --apply` (+ routing from its own `setup.sh --write-provider-routing`),
its `scripts/attest.py` on a pty (owner key, both billing statements), its installed CLI (`learning-register`), then the L5 run on it
(`drive_lifecycle(..., earlier_release=True)`, added to `scripts/e2e_checks_run.py` by splitting `lifecycle_scene`; no assertion changed).
The update is HEAD's `./setup.sh --answers` with `upgrade_records: true`: `--update` alone refuses (`records_need_owner_choice`) until the
owner chooses the records upgrade. This proves the DATA upgrade from the release, not its old wizard (R6, an owner check).

## Per ID
- L12.1 -> `l12_1_update_from_v0_3_{1,2,3,4}_succeeds_and_the_full_run_passes_again` (4 scenes, ~76 s each, parallel). Reads: the tag's VERSION from git
  objects vs the old collector's runtime version, ledger counts, the CLI status after, the process table, then the L5.1-L5.3 judges on the updated project.
- G7 -> `g7_update_from_v0_3_{1..4}_keeps_counts_by_kind`: ledger rows by table and by record type, read-only sqlite before and after.
  Every table of the old ledger keeps exactly its rows, except the five a setup's own two provider checks add to (at most +4 each).
- L12.6 -> `l12_6_over_512_admission_records_and_128_mib_still_open_dispatch_and_upgrade` (v0.3.1, ~130 s). The ledger is grown by copying the release's own
  rows (300 headroom, 300 billing, 200,000 events of a real hook event; ledger > 128 MiB) while no process of the release runs: the one direct write
  into an old ledger. Then update, backup file over 128 MiB, ledger event count unchanged, billing probe runs, L5 run passes.
  NOT PROVEN (needs a decision from the primary: split L12.6 or accept): (a) runs left by 0.2.x: a 0.2.2 run left open, taken over by 0.3.1 and
  then HEAD makes the update refuse (`learning-register --upgrade-ledger`: "a child run is active; nothing was changed"); this is the documented rule
  that a fresh 0.2.x run still counts as running for a day (CHANGELOG 0.3.2), so it cannot be shown green without an aged record (no clock control);
  and the refusal leaves the collector down (status_collector_unavailable) after step 5 failed: the half-install bug fixed elsewhere (decisions 260-261).
  (b) the masked busy probe (needs the same slot held); (c) the protected copy without role files (services in this world run from the setup's source,
  not from the owner's protected copy).
- L12.7 -> unchecked, `deferred: true` with a reason. Needs a claim refused on v0.3.2 with long history (the old service leaves the lease), then the next service closing it.
  `tests/test_e2e_requirements.py` takes the `deferred`/`reason` keys (the same acceptance as T7 S6 commit f30c3222).

## Faults (`--fault NAME`, test-only boot-hook source rewrites applied only to the update's world, after the history is built)
- `update_loses_history`: the hold-register migration also deletes `run_provenance`. G7 red, L12.1 stays green.
- `admission_capture_tiny`: the admission read of the updated build is cut to one row. L12.1 and L12.6 red.

## Findings
1. Probe right after an update on the long history was once refused `collector_code_stale` (collector status said not stale; the same probe passed seconds later).
   The check retries the probe for at most 90 s and records `probe_after_wait`; not seen on the small scenes.
2. See (a) above: update refused by a fresh 0.2.x leftover leaves the collector unavailable.
