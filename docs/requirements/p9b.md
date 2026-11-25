# P9b — confirming-check follow-ups

Accepted scope: the user's P9b follow-up brief and the P9 confirming review on
accepted commit `38911cd4`. No VERSION or runtime digest catalogue change.

### P9b-1 — Only definitive verifier results are cached

An unsafe verifier executable or parent (symlink, wrong owner, writable mode),
or an unavailable, killed, out-of-memory or otherwise failed verifier, must
raise in the maintenance authority's `raise_verifier_failures` mode. Its row
stays unchecked, charged and retryable without restart. Only a clean successful
verification or clean definitive ssh-keygen signature-invalid result can be
cached. Existing non-maintenance callers continue to fail closed.

Review fix round on `9333772e`: exit 255 with stdout exactly
`Could not verify signature.` (allowing its line terminator) is definitive
invalid, including a well-formed signature from the previous owner key. Exit
255 with deterministic malformed-signature diagnostics (`invalid format`,
`decode base64`, `missing footer`, `missing header`, `incomplete message`, or
`namespace does not match`) is also definitive invalid. In either case,
stderr indicating `cannot open`, `no such file`, `permission denied`,
`allocate`, `out of memory`, `internal error`, or `killed by signal` takes
precedence and leaves the row unchecked. Unsafe prechecks, timeouts, signals,
other exit codes and all other ambiguous results remain non-definitive.
Real ssh-keygen regressions must prove wrong-key and truncated signatures
are cached invalid without verification on the next maintenance pass, while
unsafe verifier metadata remains retryable.

### P9b-2 — Candidate membership contains only attestations

`temp.capture_candidates` contains billing attestations only. Insert, update,
restart and projection rebuild preserve that restriction. Candidate count and
the per-open candidate rebuild's SQLite VM work are independent of unrelated
non-attestation history. Admission still captures all relevant evidence families
under its existing time, project, source and quota rules, excluding unrelated
history. Both public capture boundaries retain bounded superseded-history work.

Review fix round: EXPLAIN QUERY PLAN must show indexed admission-union access
without a full records scan. Each union branch supplies at most the capture
limit (including its overflow sentinel) before the final rowid sort and
record/signature joins. This preserves the exact globally ordered prefix and
overflow refusal while bounding the rows materialized for that final sort;
the existing SQL work/deadline budget still governs scope filtering.

### P9b-3 — Record deletion restores surviving predecessors

Deleting a billing attestation by any record-deletion path rebuilds the derived
supersession state for its project/account/provider. Deleting the newest valid
row immediately restores the latest surviving verified predecessor as a capture
candidate, without restart or re-verification. Other accounts and providers
remain independent. Deleting superseded rows must preserve bounded sweep work.
