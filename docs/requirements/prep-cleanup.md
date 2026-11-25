# Preparation cleanup (Unit 3.9 slice 4)

Authority: remaining-work slice 4 and owner decision 309 (B plus a warning).
Base: `afb6f610` (0.8.66). VERSION and the digest catalogue stay unchanged.

| ID | Accepted clause | Matching tests |
| --- | --- | --- |
| PREPCLEAN-1 | Fail physical removal twelve times; attempt thirteen really removes the folder. A remover's return or exception alone is not proof. Until observed absence, the preparation slot, byte reservations and RS-B M2 preparation/entry holds remain charged. | `CodexPreparationTests.test_PREPCLEAN_1_twelve_physical_failures_then_observed_removal`, `test_PREPCLEAN_1_missing_child_is_not_workspace_removal`, `test_PREPCLEAN_real_cleanup_keeps_host_and_slot_until_thirteenth_removal`; `PreparationCleanupLifecycleTests.test_PREPCLEAN_pending_cleanup_defers_idle_replacement_then_removes_and_replaces`; existing `DriverMemoryTests.test_M2_unknown_cleanup_keeps_both_holds` |
| PREPCLEAN-2 | Keep asking in the background without an attempt limit. Pauses grow monotonically from one second, capped at sixty seconds; no work is resubmitted before its deadline. Store only capped delay/deadline, so retries do not grow an exponent or attempt counter indefinitely. Pending cleanup blocks automatic idle/exhaustion replacement, including replacement for another stuck slot. Once removal is observed, ordinary idle replacement resumes. | `GivenBackPreparationTests.test_PREPCLEAN_2_retries_continue_past_twelve_with_capped_monotone_backoff`; `StuckSlotTests.test_M4_pending_cleanup_keeps_custody_and_defers_replacement`; lifecycle test above |
| PREPCLEAN-3 | While cleanup is pending, status and the terminal warning list say plainly that a preparation folder could not be removed, removal keeps being retried, and its slot remains held. Clear the warning after confirmed cleanup/retirement. Do not expose folder paths, exception text or raw logs. | lifecycle test above; `test_PREPCLEAN_3_plain_warning_and_clear`; `CodexPreparationTests.test_PREPCLEAN_3_capacity_transport_warning_and_clear` (including wire bound + 1), `StatusWarningTests.test_PREPCLEAN_3_status_warning_list`, `test_PREPCLEAN_3_status_warning_reaches_terminal_list` |

The documented remaining hold at the idle-replacement boundary is intentional:
the same live host and original cleanup worker retain custody. No durable folder
handoff is invented and no fresh host silently gets capacity in its place. A
permanently refused removal keeps that host, slot and warning indefinitely.
Explicit service stop/crash remains the RS-B M2/M5 boundary: memory accounting
tracks in-process capacity, not durable disk quotas. This slice does not add
crash recovery of unclaimed preparation folders. Ordinary automatic replacement
must not take that boundary while folder cleanup remains pending.

Tests use events and an injected monotonic clock rather than waiting through
production backoff. Existing assertion replacements are recorded in
[assertions.md](prep-cleanup/assertions.md); evidence is in
[STATUS.md](prep-cleanup/STATUS.md).

The live capacity report adds optional `run_slots.cleanup_pending`, using the
existing run-slot diagnostic range 0..64. Old reports without it remain readable.
This is an owner-supplied count, not a new stored record, operation or admission
limit. `learning-status` exposes it as `project_service.cleanup_pending` and a
fixed plain sentence in `project_service.warnings` and the summary; the setup
terminal renders that sentence with its `warn` list entry. Both authorization
and response decoding reject a count past the wire bound (65).
