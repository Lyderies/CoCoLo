# B7a status projection (CoCoLo 0.4.0)

Source: `~/Code/temp/orchestrator-child-liveness/ANALYSIS-0.4.0-rsb-b4-b7.md`, section 7.
Scope: RS-B-B7-1, RS-B-B7-2, RS-B-B7-3, RS-B-B7-6. Decision 129.1 A is implemented by RS-B memory holds: every kind is tracked.
B7b (`recovery_pending`, stale readings and budget measurements) is excluded. No schema or lifecycle writes.

## RS-B-B7-1

> (B7a) `project_holds` per kind: `held`, `releasing`, `past_due` (null for evidence-only kinds), `next_eligible`,
> `oldest_taken_at_ms`, zeros and nulls for empty kinds, never an omitted key (RS-B draft 3 §10); memory kinds tracked by B2.

Acceptance: `history.holds_report` returns `project_holds` with every kind: `driver_entry`, `preparation`, `lease`,
`reservation`, `orchestration`, `open_binding`, `result_custody`. Every kind has exactly `tracking`, `held`, `releasing`,
`past_due`, `next_eligible`, `oldest_taken_at_ms`. `tracking` is `tracked` for every kind (B2 memory holds and B6 orchestrations).
Empty counts are zero and timestamps null (orchestration `past_due` is zero).
`held` counts only held rows; `releasing` only releasing rows; oldest is the minimum taken time of held rows only.
Per draft 3 §10, lease, reservation and open_binding are evidence-only: `past_due` and `next_eligible` are always null.
Custody deadlines apply only to committed/recorded contexts or legacy committed reservations; an unfinished execution
obligation blocks custody eligibility. Empty timer kinds have `past_due: 0`, `next_eligible: null`. Effective time is
max(wall, persisted result floor), read without moving the floor. Released rows are excluded. Everything is project scoped.

## RS-B-B7-2

> (B7a) custody: live count, context bytes against MAX_LIVE_CONTEXT_BYTES (256 MiB, decision 90.3), nearest
> `retained_until_ms`, awaiting-verifier count, `legacy_no_context`, `ended_by_update_unrecorded` (RS-D-11, T1).

Acceptance: `result_custody` has exactly `live`, `context_bytes`, `max_live_context_bytes`, `retained_until_ms`,
`awaiting_verifier`, `legacy_no_context`, `ended_by_update_unrecorded`. Live and bytes use context states other than ended/expired,
including charged retiring/repair_required rows. Nearest retained time is the minimum non-null retention among live contexts.
Awaiting verifier counts live recorded worker outcomes with no verification_binding or operational QA row, excluding verification tasks.
Legacy counts live legacy_no_context rows. Ended-by-update counts unrecorded contexts with a custody release
`orchestration_closed` whose latest verified orchestration checkpoint is interrupted and belongs to an earlier runtime;
ordinary closes and recorded results do not count (including legacy rows whose admission receipt predates the context association). It remains visible after retirement while the context/source rows remain.
All empty counts/bytes are zero and the empty deadline is null. No body/context file contents, paths, handles, digests,
request IDs, evidence or foreign project data are returned.

## RS-B-B7-3

> (B7a) the D3 repair list, operator-needed first, bounded with `truncated`; each line a plain reason and what to do.

Acceptance: `result_retirement_repairs` has exactly `entries`, `truncated`. At most 20 entries, each with exactly
`reason`, `action`, `operator_needed`. Fixed plain language describes a directory blocking removal (inspect and remove the
unexpected directory, retry status), or cleanup being retried (wait, then check storage permissions/space if it persists).
Use the D3 repair reader, omit all private names/identities, order operator-needed entries first. `truncated` is boolean
and reflects additional D3 entries. Empty entries is an empty list and truncated is false.

## RS-B-B7-6

> The response check grows with the new keys in one change with the report (same runtime on both ends); older
> clients are not served the new op shape (B4m-2: a new history operation `history.holds_report` rather than new keys in
> `history.capacity_report`, recommended).

Acceptance: new operation `history.holds_report`, empty payload, request_id `holds`, strict closed response checking
at every nesting level (including types, nonnegative integers excluding bool, nullable timestamps, exact kinds,
fixed repair text, list bound and ordering). Top level is exactly `project_holds`, `result_custody`,
`result_retirement_repairs`. Authorize only enrolled project primaries/owner, under existing project/runtime gates.
HistoryClient.holds_report and client_runtime.holds_report_client use the existing project endpoint and enrollment.
`learning-status` includes these three sections plainly; each is null when the project service is unavailable, with no
store fallback. `history.capacity_report` keeps its existing reply shape. B4m reply-shape table integration is deferred
until merge because that module is absent here.

Validation and requirement-to-test map: [b7a/validation.md](b7a/validation.md).
Existing assertions are never edited; if any must change, stop and record old -> new in b7a/assertions.md.
