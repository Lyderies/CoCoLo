# D4 publication material and replay

**RS-D-D4 — Publication material and replay.**
The prepare transaction stores the exact prepared material (the canonical bytes whose keyed digest is `prepared_digest`) in
`project_result_publications`; the commit transaction removes it, and so does the transaction that records `retiring`. A
failed publication with the service live is retried from the kept packet (the live lane keeps it until its own commit) and
commits once, reusing the final body file, with `prepared_digest` and `retained_until_ms` unchanged. A publication is committed
from the store, by a successor or by settlement arbitration, only while it is still eligible, decided inside the commit
transaction: the reservation is `reserved`, its RS-B obligation row is `held`, the context is `bound`, and the stored material
verifies (its digest, binding, assignment digest, terminal record, usage record and the final body's digest); no deadline is a
condition. An ineligible or altered publication is refused (`history_conflict` when altered) and only finishes cleanup. Before
the account is asked to settle an attempt the project writer arbitrates it: `recovered` (a verifiable body is committed
first), `unrecoverable` (the obligation row goes `releasing`, so no publication of that attempt, live or replayed, commits
afterwards) or `settling` (a `releasing` row or a settlement row already exists: nothing is committed, cleanup follows). A
stop before the body's final link leaves a run that RS-B's settlement ends lost, with no half-recorded state; a result is
lost only when the service stops before a verifiable final body exists (RS-D-12 as narrowed; no numeric bound, owner
decision 90.1 A and 90.2 A). A replay that commits a claim that has a live job is adopted by the lane once.
Source: DESIGN-0.4.0-RS-D.md draft 3 s1-s3, s6, draft 4 s2-s4, rows P1, R1-R6, R3', R4', P2', P3', X1-X3, L1-L3; DESIGN-0.4.0-RS-BD-shared.md S-2, S-3; measurements/d4/requirements.md.
