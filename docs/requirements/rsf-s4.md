# RS-F S4: legacy run slots under M4 R3

Requested worktree: `feat/rsf-s4-slots`, base `ace235a4` (0.8.56), as supplied
by the user; no git command used to verify or change it.
Sources: `~/Code/temp/orchestrator-child-liveness/ANALYSIS-0.4.0-rsf-migration.md`,
section 3 first bullet and section 4 S4;
`~/Code/temp/orchestrator-child-liveness/PLAN-0.4.0.md:1486-1490`, M4 R3.

Runtime paths below are relative to
`packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.
Line numbers describe the code read before any implementation change.

## Current behavior and exact locations

| Requested reference | Current code and effect |
| --- | --- |
| `active_run_capacity.py:176-208` | `LEGACY_RUN_MAX_MS` is 24 hours; `crashed_claim` treats every `legacy_unknown` claim as crashed strictly after that age, without dispatch or process proof. |
| `learning_store.py:2997-3015` | In this worktree adoption is `_initialize_run_holds_locked` at `3002-3020`: install/adopt holds, release proven `service_lost`, then run the due sweep. `2997-3000` belongs to admission-evidence installation. Capacity backfill is at `2959-2971`; historical launch fencing at `2883-2895` does not infer unsent from missing history. |
| `learning_store.py:3959-3975` | `drained` calls the source-image drain check at `3959`. `earlier_version_runs` at `3964-3980` counts through `crashed_claim` and promises the newest claim time plus 24 hours. |
| `learning_store.py:11945-11960` | Status assembly ends at `11948`; `_slot_eligible` at `11950-11966` promises claim time plus 24 hours plus 1 ms for every legacy slot. |

## Acceptance requirements

**RSF-S4-1 — Evidence classification.** Adoption and subsequent sweeping must
classify legacy slots using retained, validated evidence bound to that run.
A missing or pruned `dispatch_intent`, a consumed binding, claim age, or a client
version alone cannot prove that dispatch never occurred. With no positive proof,
the slot stays held, including after 24 hours. Reopening and retention must not
turn missing evidence into permission to release or launch again.

**RSF-S4-2 — Proven unsent.** Positive proof that the run was never dispatched
releases its slot immediately during adoption or the next sweep, without waiting
for a claim-age timer. For example, the source analysis identifies a validated v1
journal entry still in `claimed` as no-spawn proof; `spawning` or `launched` is
not such proof. Record the release decision durably and idempotently; do not
invent a terminal result or cleanup receipt.

**RSF-S4-3 — Bridged deadline.** A retained dispatch intent that identifies this
bridged run and its recorded deadline makes the slot eligible after that deadline
plus `CRASHED_CLAIM_GRACE_MS` (3,600,000 ms). Preserve the current strict boundary:
it is held at deadline + grace, and eligible at deadline + grace + 1 ms. Test
immediately before, exactly at, and immediately after that boundary. Do not
substitute claim + 24 hours or a fresh deadline computed during adoption. A
bridge client label without a retained deadline is insufficient. Preserve the
existing two-clock protection for current, nonlegacy claims.

**RSF-S4-4 — Deadline-free native child.** A native Claude run without a recorded
deadline remains held until positive process evidence establishes that its child
is gone or an authorized owner confirms release. A positive alive observation
past 24 hours must remain held through adoption, sweeping and restart. Time
alone, missing evidence and failure to contact the child never release it.
This slice must not introduce an unauthenticated owner-release path.

**RSF-S4-5 — Plain, accurate status.** Status must say why slots are held using
fixed plain explanations: waiting for the recorded deadline and one-hour grace;
waiting for process evidence or owner confirmation; or dispatch evidence is
missing. Evidence-only holds have no automatic eligibility/finish time. Mixed
status must not promise that all runs finish at a finite time when any held run
needs evidence. Reasons must expose no paths, run/bridge IDs, handles, digests,
provider account values or raw evidence. Reading status performs no release.

**RSF-S4-6 — One decision across paths.** Adoption, due checks, sweeping,
capacity counting and status must agree on the three cases. Repeat adoption,
sweep and reopen without duplicate releases or changed receipts. Preserve
historical launch fencing and signed record bytes. Keep code limited to the
smallest change supporting these rules; no unrelated migration or probe work.

**RSF-S4-7 — Validation and assertion boundary.** Write store-level regression
tests before implementation for pruned intent, positively proven unsent,
deadline/grace boundaries and a native child alive past 24 hours; run red before
code changes. Use only named unit modules from `packages/codex-agents`, each
under 300 seconds. Do not open sockets or invoke git. Record external socket/CLI
commands in [validation.md](rsf-s4/validation.md). Never edit an existing test
assertion: if one must change, stop and list old → new in
[assertions.md](rsf-s4/assertions.md).

Status: implemented after primary decision 164 approved the enumerated assertion
changes. Behavioral red/green results and primary-only commands are recorded in
[validation.md](rsf-s4/validation.md).

**RSF-S4-8 — Owner confirmation.** A deadline-free native Claude slot awaiting
owner confirmation shows a plain reason and the exact owner command for that
slot. The command uses the existing detached-signature owner-record authority,
releases exactly its project's named slot, records the confirmation atomically,
and returns the same durable decision on retry. An unsigned, forged, other-project
or non-owner request cannot release any slot. Status reads never confirm release.
