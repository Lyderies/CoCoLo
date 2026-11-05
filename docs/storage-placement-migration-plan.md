# Storage placement and migration plan: review draft

Status: requirements and corrected build-plan draft for 0.3.0. No split-layout
migration, launch-claim protocol or privileged service is enabled by this document.
It needs independent planning verification and the implementation exits below.
The [service API boundary](service-api-development.md) remains required.

## Current source facts

The legacy ledger accepts 27 record types and eight persisted sources.
`PRODUCER_AUTHORITY` is a limit on public append, not the complete set of
controlled business writers. Admission, dispatch, profile and verification records
also have dedicated internal writers. Preserve their original sources; in
particular, legacy dispatch intent/result use `lifecycle`.

Canonical content and signatures exclude database ingestion metadata.
Imported content bytes, content digest, IDs, project partition, source,
source sequence, observed and ingested times, expiry, late flag and lineage stay
exact. A new-write validator is not an importer: legacy schema-version equality
and missing content timestamps must not cause byte rewriting.

The legacy append counter is `MAX(retained source_sequence)+1` per project.
A duplicate keeps first-ingest metadata; deletion can remove a tail.
Decision code uses persisted sequence order for cases, retries, reviews,
canaries and promotions. Owner grants also compare their issue time with
prerequisite ingestion times. A new local counter or replica arrival time cannot
stand in for this authority order.

The current project-history prototype supports only five types and has no full
legacy import, lineage or retention implementation. Its strict row checks and
source-conflict rules must be reconciled with legacy reads before migration.
The actual normal-client inventory must include indirect helpers and
provider/bridge/hook/setup/attestation paths, not just direct CLI branches.

## Placement to finish during review

One account owner retains the global digest key, owner history/signatures,
registration, provider/account restrictions, shared admission/window/bootstrap
facts, consumed claims and central terminal/cleanup receipts. A project store
never creates a second full account state or key.

The candidate record map is below. It is a proposal to verify, including its
ordering obligations, rather than an accepted schema:

| Candidate owner | Record types |
| --- | --- |
| Account authority | provider_identity, provider_headroom, provider_billing, billing_attestation, admission_decision, probe_admission |
| Account authority with checked project copies | feedback, grader_calibration, owner_grant |
| Project canonical history | event, outcome, evaluation_case, evaluation_assignment, qa_evidence, evidence_review, evaluation_trial, candidate, comparison, canary_report, promotion, learning_export, dispatch_intent, dispatch_result, host_run_profile, verification_binding, observation_unavailable, provider_usage |

Verify the last two types' actual consumers before selecting their owner.
`record_observation_unavailable` explicitly describes diagnostic, non-admission
evidence; usage is recorded for one dispatched run. Other consumers and future
account restrictions still require a complete audit.

The final map must also place sessions/provenance, host bindings, signatures,
lineage, consumed grants, repair/fallback receipts, operational QA/check receipts,
orchestrations/checkpoints, health/ingestion, managed files/backups and new journals.
No writable replica may become another owner of authority. Valid cross-store
lineage needs a checked reference or immutable receipt; merely refusing all such
edges does not constitute a complete importer.

Resolve cross-store ordering before implementing this map. Two candidate approaches
are central ordering/decision metadata for authority-sensitive operations with
project canonical storage, or explicit typed prerequisite/decision receipts with a
proved causal protocol. A replica apply counter is insufficient. Compare these
against the original decision code, current transaction boundaries and golden
fixtures. Select a concrete technical design with its failure and freshness rules;
keep material native-authentication choices separate and open.

Opaque handles are labels that must bind to independently authenticated caller
context. Their presence in an owner-only project store outside writable repositories
does not alone establish a secret-exposure defect. Define custody and lost-reply
recovery from the actual threat and authority boundary. Preserve documented
same-user read limits, exact legacy identities and pending work. Do not introduce a
generic HMAC oracle or a per-project account key.

## Conservative launch and completion protocol

1. Stage bounded typed inputs through closed operations and server-issued
   references. No arbitrary path, method, SQL, oversized metadata envelope or
   rejected model body is a transfer mechanism.
2. Extract a business-specific account unit. Existing public methods reject outer
   transactions; wrapping them in another transaction is insufficient. Verify slow
   immutable inputs outside the writer, then recheck actual caller facts, current
   owner/policy generations and the original shared restrictions, caps, bootstrap
   and clock inside a short atomic admit/reserve/journal operation.
3. Persist the exact claim receipt and local intent before requesting consumption.
   Define complete project/session/runtime/supervisor/run/attempt and authority
   bindings, immutable request IDs/digests and conflict handling.
4. Consume centrally once. An exact retry returns known consumed state and its
   stable receipt, without another launch permission. A lost first response remains
   ambiguous. Do not retrieve a cached first permission because the same generation
   has no local spent marker.
5. Persist a protected unique local spent marker before at-most-one OS spawn.
   Missing rows, lease expiry, absent PID, controller death and restart never
   establish nonexecution or permit another launch. No database transaction spans
   remote persistence, external verification or the OS spawn.
6. Commit local terminal metadata and its outbox together; finalize the exact claim
   and result digest centrally with an idempotent receipt. Terminal acceptance and
   verified process-tree cleanup are separate. Unknown cleanup retains active
   capacity; window admissions retain their original accounting semantics.
7. Define lost-open identity recovery explicitly. The current open generates a
   random raw handle and stores its digest. Recovery cannot create another run or
   admission after losing that reply. Evaluate custody options with exact replay,
   generation and revocation rules rather than assuming a handle is native proof.

## Migration and observable exits

First produce a complete type/table/file/client census and decision-equivalence
fixtures. Preserve historical metadata and distinguish new authority, local ingest,
imported legacy and delivery/journal sequence domains. Retention and project
deletion cannot resurrect a spent permission or discard unresolved work.

Build a proven drain and old-writer fence, an immutable verified source image, and
owner-only staged stores with a resumable migration journal. Old open processes
must actually be drained; a schema integer alone cannot fence an existing
connection. Preserve canonical/signature bytes and global keys without re-signing
or inferring native ownership for old data.

Publish only verified staging through an explicit activation protocol that
prevents two writable authorities and partially activated projects. Define crash
cuts around fencing, image, staging, verification, activation and first new write.
After new authority writes, an old snapshot cannot silently replace them.
Unresolved old execution remains conservative and never automatically relaunches.

Required exits include real commit-failure/SIGKILL/restart/lost-reply tests,
same-account last-slot/bootstrap and generation races, complete byte/metadata/order
comparisons, explicit duplicate/conflict handling, stalled-writer project isolation,
and normal-client source/integration checks with no SQLite fallback.

Independent local timers and cancellation cannot wait for either persistence owner.
Native issuer/profile/MCP/FD custody, verified launch-caller facts and native Claude
control remain unselected material gates. Both Codex-child paths come first; all
agreed provider/primary paths, installed diagnostics/fresh probe and real
two-project release validation remain mandatory.

