# T7 slice S4: updates from earlier releases (L12.1, L12.6a, L12.6b, L12.7, G7)

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
- G7 -> `g7_update_from_v0_3_{1..4}_keeps_counts_by_kind`: the ledger's rows by table and by record type, read-only sqlite before and after, now by
  IDENTITY (each row's primary key, each record's id), not by count against an allowance. Every row the earlier ledger held must still be there
  (nothing lost, by table and by kind); the setup's own additions are the rows that are new, identified separately (`setup_additions`): only the
  the setup's additions are exact, as measured on real updates from all four tags: `SETUP_ADDS_TABLES` (observation origins 2, observation requests 2,
  admission evidence 2, stored files 1) and `SETUP_ADDS_KINDS` (provider identity 2, billing 1, headroom 1; records 4); every other table and kind of the earlier ledger gains 0
  (tables a migration made new, e.g. run_holds, project_wake, and admission_evidence on v0.3.1, are not the earlier release's); each table's count must equal its earlier rows plus the additions. Deleting two old provider identities while the setup adds two new ones is red (unit test
  `test_two_old_provider_identities_deleted_while_the_setup_adds_two_fails`, red against the earlier judge).
- L12.6a -> `l12_6a_over_512_admission_records_and_128_mib_still_open_dispatch_and_upgrade` (v0.3.1, ~100-130 s). The ledger is grown by copying the release's own
  rows (300 headroom, 300 billing, 200,000 events of a real hook event; ledger > 128 MiB) while no process of the release runs: the one direct write
  into an old ledger. Then update, backup file over 128 MiB, event count unchanged, every admission record id of the earlier ledger still there after the update AND
  after the project opened and dispatched (final read), more than 512 admission records in the last-day window at that point, the billing probe on its FIRST attempt
  (no retry), L5 run passes.
  LIMITATION (synthetic volume fixture): the history is copies of one admission template and one real hook event with changed ids and a padded model name. It proves the
  stated thresholds (rows over 512, a ledger over 128 MiB) and that the migration, backup and open paths handle that volume. It does NOT show authentic history
  diversity or the growth of tables that relate to historical runs and links (200,000 events keep one template's relationships): an upgrade whose cost grows with
  historical runs or links would stay invisible here.
- L12.6b (split from L12.6 by decision 266) -> unchecked, `deferred: true`. Three cases, none built:
  (a) runs left by 0.2.x: a 0.2.2 run left open, taken over by 0.3.1 and then HEAD makes the update refuse (`learning-register --upgrade-ledger`: "a child run is active; nothing was changed"); this is the documented rule
  that a fresh 0.2.x run still counts as running for a day (CHANGELOG 0.3.2), so it cannot be shown green without an aged record (no clock control);
  and the refusal leaves the collector down (status_collector_unavailable) after step 5 failed: the half-install bug fixed elsewhere (decisions 260-261).
  (b) the masked busy probe (needs the same slot held while the probe runs); (c) the protected copy without role files (services in this world run from the setup's source,
  not from the owner's protected copy).
- L12.7 -> unchecked, `deferred: true` with a reason. Needs a claim refused on v0.3.2 with long history (the old service leaves the lease), then the next service closing it.
  `tests/test_e2e_requirements.py` takes the `deferred`/`reason` keys (the same acceptance as T7 S6 commit f30c3222).

## Faults (`--fault NAME`, test-only boot-hook source rewrites applied only to the update's world, after the history is built)
Real green -> red runs with `scripts/e2e_lifecycle.py --execute <checks> --fault NAME` (final code, results as run on 2026-11-25; the same checks without a fault: all green):
- `update_loses_history` (the hold-register migration also deletes `run_provenance`): G7 v0.3.1 RED ("no table of the earlier release lost a row"), L12.1 v0.3.1 stays green.
- `admission_capture_tiny` (the updated build's admission read is cut to one row): L12.1 v0.3.1 RED (a later step refused, G1 unexpected exit); L12.6a RED (the first-attempt billing probe refused, G1).
- `admission_history_dropped` (new: the same migration deletes the admission records, headroom and billing): L12.6a RED ("the update kept every admission record of the earlier release"); G7 v0.3.1 RED.
All faults the slice declares were shown red by `--execute`; none is only a source-anchor unit test (the unit test still checks each anchor exists once). Unit-only: no fault shows
"two old identities deleted while the setup adds two" (a product migration cannot be made to do it with a one-line rewrite): it is the unit guard test.

## Findings
1. `collector_code_stale` right after setup reported success (seen once by the first S4 builder, on the long history; the collector's own status said not stale, the same probe passed seconds later).
   NOT REPRODUCED in this round: about 50 runs of the long-history scene (1, 3, 8 and 10 in parallel on a loaded machine) plus the four small scenes, every first-attempt probe
   passed and `learning-status` right after the update said ready, this build, not stale. The probe is now judged on its FIRST attempt (no retry); on a refusal the scene prints
   `first_probe_refused` with a 60 s trail (status, collector pid and age, a probe every 3 s) so the next sighting names its cause. Who can say it (product, not harness):
   `client_runtime.py` `billing_probe_client` (lines 922-936): (1) `CollectorClient(...).health()` raising `CollectorUpgradeRequired` or `CollectorBuildConflictError`
   (`collector_client.py` 244-275: the answering collector reports a lower version or schema, or a digest other than the probe client's files) with the diagnosis `stale`
   (`collector_client.py` 105-117), or (2) the collector's own reply `runtime_stale: true` (`learning_store.py:13980` -> `collector_client.py:68-78`: the runtime files under the
   collector's root hash differently now than at start, and a file that cannot be read counts as changed). The harness writes no runtime file after setup (no fault
   in the green runs) and runs the same health call as status, so a transient refusal that status does not show is the collector's answer changing between the two calls
   (a replaced collector, or a runtime file momentarily unreadable or rewritten), a product readiness question. It is NOT shown harness-only, so the check stays strict: red when it happens.
2. Under load (8 or 10 L12.6a runs in parallel on this machine) the lifecycle's later step `learning-open-host-run` was refused in about one run in three (`open_account_busy`), not
   `collector_code_stale`; with 1-3 in parallel 11 of 11 runs were green. It is the L5 lifecycle's own step (no S4 code), judged by G1 as an unexpected refusal; run S4 scenes at most about 3 in parallel.
3. Update refused by a fresh 0.2.x leftover leaves the collector unavailable: see L12.6b (a).
