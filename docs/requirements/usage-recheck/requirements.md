# usage-recheck: requirements (unit usage-recheck, for 0.8.56)

Analysis: `ANALYSIS.md` (this folder). Problem: a provider marked exhausted with a far measured reset is never
re-read, and a fresh good reading would not clear it anyway, so an early provider reset (live: Claude, owner used
an early reset, next_reset Sat 2026-11-28 01:00) leaves CoCoLo skipping that provider until the old reset time.

Named constants (owner to confirm the numbers): `USAGE_RECHECK_INTERVAL_MS = 3_600_000` (60 min),
`USAGE_RECHECK_FAR_RESET_MS = 3_600_000` (a reset more than 1 h away is "far").

### USAGE-RECHECK-1 - When a re-check is due
For each provider the classifier reports `recheck_due_at_ms`: when the state is `exhausted` and its
`next_reset_ms` is null or more than `USAGE_RECHECK_FAR_RESET_MS` after the evaluation time, it is the newest
usage reading of that provider/account (`observed_at_ms`, at or before evaluation time) plus
`USAGE_RECHECK_INTERVAL_MS`; otherwise null. A re-check is due when `recheck_due_at_ms <= evaluation time`.
**Acceptance:** exhausted with a reset 3 days away and the newest reading 61 min old -> due; 10 min old -> not due
(due at reading + 60 min); exhausted with a reset 30 min away -> null; ample/constrained/unknown -> null.

### USAGE-RECHECK-2 - The re-check reading itself
Codex re-checks with the existing non-spending observation (`account/rateLimits/read`). Claude re-checks with one
billing probe: while a re-check is due (USAGE-RECHECK-1), the probe's admission does not apply the exhausted gate;
every other probe gate stays (attestation, paid usage, API keys, managed config, cooldown, reuse). When no
re-check is due the exhausted gate still refuses the probe (keeps `test_bridge.py`
`test_exhausted_window_blocks_probes_at_ninety_percent`). A probe is not a run: no work, no run cap.
Cost (flag for owner): one Haiku low-effort single turn per re-check, at most one per provider per interval.
**Acceptance (real account service):** Claude exhausted (rejected, reset 3 days away) by a reading 61 min old ->
`provider-observe --provider claude --billing-probe` launches one probe and stores its records; same with a
reading 10 min old -> refused `provider_exhausted`, nothing launched.

### USAGE-RECHECK-3 - An early reset clears a measured restriction (NEEDS OWNER DECISION: changes RT-RX-5)
A later reading (strictly later `observed_at_ms`, at or before evaluation time) of the same provider, account and
window (same `scope` and `window_minutes`) that rates that window non-restrictive AND carries its own explicit
`resets_at` different from the restrictive window's proves the window reset early: it supersedes that window's
restriction (and a Claude `rejected` status bound to that window). This applies only while the restrictive
window's reset is more than `USAGE_RECHECK_FAR_RESET_MS` away: a near reset is waited for, so RT-RX-5 and
`tests/test_providers.py:1171-1173` ("a later, favourable read does not clear a window that has not reset", reset
5 s away) are unchanged. A later reading with no reset time, for another
window, for another account, or older/equal in time does not clear it; RT-RX-5 is otherwise unchanged.
**Acceptance:** Claude `rejected` seven_day window (reset +3 days) at t1, later `allowed` seven_day 2% (reset +7
days) at t2 -> not exhausted; later five_hour-only reading -> still exhausted; later resetless reading -> still
exhausted (existing RT-RX-5 test); same for Codex 95% -> 10% on the same window with a new reset.
Option B if the live reading after an early reset keeps the old reset time: any later measured non-restrictive
reading of the same window supersedes it (changes `test_route_exhausted.py:84`).

### USAGE-RECHECK-4 - Who triggers it
(a) The primary host's route (`learning-route`, and the route re-pick at `learning-open-host-run`): when the
snapshot says a re-check is due for a provider, take that one reading (USAGE-RECHECK-2), then re-read the snapshot
and route on it. (b) Explicit: `provider-observe --provider <p>` (Codex) / `--billing-probe` (Claude) from the host
is the re-check command. (c) The owner's `learning-status` shows each provider's state, `next_reset` and
`recheck_due_at`; it takes no spending reading (the owner terminal cannot run probes,
`tests/test_owner_terminal_flow.py:66`).
**Acceptance (real account service):** route with Claude due -> exactly one probe; early-reset reading -> route
may pick Claude; still rejected -> Claude skipped as today; a second route straight after -> no second probe.

### USAGE-RECHECK-5 - Bounded, recorded, never real work on the exhausted provider
Selection uses only the snapshot read after the re-check; no worker or verifier is ever routed to a provider that
snapshot still calls exhausted. At most one re-check per provider per route call, and at most one per provider per
`USAGE_RECHECK_INTERVAL_MS` across calls (the stored reading's time is the clock, so a restart does not reset it).
A re-check never fails the route: a refused or failed re-check routes on the existing snapshot. The route's
availability audit records `recheck: {<provider>: {"outcome": "cleared"|"still_exhausted"|"failed:<reason>",
"observed_at_ms": int|null}}`; the probe's records are stored as every probe's are.

### UX-HCU-1 - host_client_unknown says what to do (LOW)
The refusal `host_client_unknown` gets its own next step: run the command from the Claude Code or Codex session
that is the project's host (or name a host that is running there). It does not say to run learning-status.
**Acceptance:** `refusal_steps.next_step('host_client_unknown')` names the host session and not learning-status.
