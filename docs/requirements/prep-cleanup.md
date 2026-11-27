# Preparation cleanup (Unit 3.9 slice 4)

Authority: remaining-work slice 4 and owner decision 309 (B plus a warning).
Base: `afb6f610` (0.8.66). VERSION and the digest catalogue stay unchanged.

| ID | Accepted clause | Matching tests |
| --- | --- | --- |
| PREPCLEAN-1 | Fail physical removal twelve times; attempt thirteen really removes the folder. A remover's return or exception alone is not proof. Until observed absence, the preparation slot, byte reservations and RS-B M2 preparation/entry holds remain charged. | `CodexPreparationTests.test_PREPCLEAN_1_twelve_physical_failures_then_observed_removal`, `test_PREPCLEAN_1_missing_child_is_not_workspace_removal`, `test_PREPCLEAN_real_cleanup_keeps_host_and_slot_until_thirteenth_removal`; `PreparationCleanupLifecycleTests.test_PREPCLEAN_pending_cleanup_defers_idle_replacement_then_removes_and_replaces`; existing `DriverMemoryTests.test_M2_unknown_cleanup_keeps_both_holds` |
| PREPCLEAN-2 | Keep asking in the background without an attempt limit. Pauses grow monotonically from one second, capped at sixty seconds; no work is resubmitted before its deadline. Store only capped delay/deadline, so retries do not grow an exponent or attempt counter indefinitely. Pending cleanup blocks automatic idle/exhaustion replacement, including replacement for another stuck slot. Once removal is observed, ordinary idle replacement resumes. | `GivenBackPreparationTests.test_PREPCLEAN_2_retries_continue_past_twelve_with_capped_monotone_backoff`; `StuckSlotTests.test_M4_pending_cleanup_keeps_custody_and_defers_replacement`; lifecycle test above |
| PREPCLEAN-3 | While cleanup is pending, status and the terminal warning list say plainly that a preparation folder could not be removed, removal keeps being retried, and its slot remains held. Clear the warning after confirmed cleanup/retirement. Do not expose folder paths, exception text or raw logs. The summary does not promise automatic replacement while a removal is pending (`replaced_when_idle` is false and the text says the service is not replaced automatically meanwhile). A pending folder whose location the service never learned (`cleanup_unlocated`) adds a second plain warning that says so and what the owner can do. | lifecycle test above; `test_PREPCLEAN_3_plain_warning_and_clear`; `CodexPreparationTests.test_PREPCLEAN_3_capacity_transport_warning_and_clear` (including wire bound + 1), `StatusWarningTests.test_PREPCLEAN_3_status_warning_list`, `test_PREPCLEAN_3_status_warning_reaches_terminal_list` |
| PREPCLEAN-4 | A pending folder removal never blocks service replacement or an update (owner decisions 309 and 129.2). The removal is recorded durably in the preparation hold row's evidence (`removal: pending`, `workspace`), written with the first refused removal and, at a graceful stop, for every preparation still owed. The record outlives the row's release (a tombstone keeps its evidence) and a crash. A successor service reads the records of other instances once its start-up memory pass is done (at most 64 at a time), retries each with the same capped backoff, shows the same warning and records the removal as proven only after observing the folder absent; only this host's own `prepare-*` folders in the scratch container are removed, anything else standing there is left alone and is not proof. | `PreparationCleanupLifecycleTests.test_PREPCLEAN_4_a_refused_removal_never_keeps_a_withdrawn_host_alive`, `test_PREPCLEAN_4_replacement_proceeds_and_the_successor_resumes_the_removal`; `DriverMemoryTests.test_PREPCLEAN_4_*` (three); `InheritedRemovalTests` (two); `CodexPreparationTests.test_PREPCLEAN_4_recorded_folder_removal_checks_ownership_and_proves_by_absence` |

Two cases are kept apart. Automatic idle or exhaustion replacement still waits
while this service owes a removal: the same live host and original cleanup worker
retain custody, and no fresh host silently gets capacity in its place. An update
or uninstall that withdraws the host never waits for a removal (the host stops as
soon as no run is held, `ProjectServiceHost.work_held`): the removal is already
recorded in the preparation hold row's evidence, which stays when the memory hold
is released at the graceful stop (`retired`) or by the successor after a crash
(`service_stopped`). The successor continues the retries and the warning until the
folder is observed absent, then rewrites the record as removed (no folder kept).
A permanently refused removal therefore keeps a warning and retries, never a
blocked service. The M2/M5 memory holds are unchanged: they account for
in-process capacity and are released at stop; only the record of the folder
survives. No schema, hold kind or release kind was added.

Limits that remain, stated plainly:

- A crash that happens before the first refused removal was recorded loses the
  record (the folder was never known to be stuck); nothing scans the scratch
  container for unclaimed folders.
- A given-back preparation whose job never learned its folder (its worker failed
  before the folder was named, or its slot thread died: `unlocated`) has nothing to
  record or retry. It keeps its slot for the life of the service and the status
  says so in a second warning (`cleanup_unlocated`, how many); the slot is freed
  when the service stops and the folder, if one exists, must be deleted by hand
  from the CoCoLo scratch area. The record cannot settle this: no path exists.
- Automatic idle replacement of a service that owes a removal still waits, so a
  successor with an inherited removal stays until the folder is gone or an
  update replaces it.

Tests use events and an injected monotonic clock rather than waiting through
production backoff. Existing assertion replacements are recorded in
[assertions.md](prep-cleanup/assertions.md); evidence is in
[STATUS.md](prep-cleanup/STATUS.md).

The live capacity report adds optional `run_slots.cleanup_pending`, using the
existing run-slot diagnostic range 0..64 (counting both its own given-back preparations and inherited removals),
and optional `run_slots.cleanup_unlocated` (the same range) for the ones without a known folder. Old reports without it remain readable.
This is an owner-supplied count, not a new stored record, operation or admission
limit. `learning-status` exposes it as `project_service.cleanup_pending` and a
fixed plain sentence in `project_service.warnings` and the summary; the setup
terminal renders that sentence with its `warn` list entry. Both authorization
and response decoding reject a count past the wire bound (65).
