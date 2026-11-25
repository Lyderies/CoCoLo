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

### P9b-2 — Candidate membership contains only attestations

`temp.capture_candidates` contains billing attestations only. Insert, update,
restart and projection rebuild preserve that restriction. Candidate count and
the per-open candidate rebuild's SQLite VM work are independent of unrelated
non-attestation history. Admission still captures all relevant evidence families
under its existing time, project, source and quota rules, excluding unrelated
history. Both public capture boundaries retain bounded superseded-history work.

### P9b-3 — Record deletion restores surviving predecessors

Deleting a billing attestation by any record-deletion path rebuilds the derived
supersession state for its project/account/provider. Deleting the newest valid
row immediately restores the latest surviving verified predecessor as a capture
candidate, without restart or re-verification. Other accounts and providers
remain independent. Deleting superseded rows must preserve bounded sweep work.
