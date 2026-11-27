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

Final targeted fix on `1e6b5e13` (confirming review `040-p9b-r2c`): a
nonzero verifier result is definitive invalid only for exit 255 with stdout
exactly `Could not verify signature.` (optionally terminated by LF or CRLF),
and stderr either empty or containing only exact allowlisted diagnostic lines.
The allowlist is the following eight lines observed from real OpenSSH 10.5p1
malformed/truncated, payload-mismatch or wrong-namespace signatures (LF and CRLF are accepted):

- `Couldn't parse signature: missing footer`
- `Couldn't parse signature: missing header`
- `sig_verify: sshsig_armor: invalid format`
- `sshsig_dearmor: decode base64: invalid format`
- `Couldn't verify signature: invalid format`
- `sshsig_peek_hashalg: parse signature object: incomplete message`
- `Couldn't verify signature: namespace does not match`
- `Signature verification failed: incorrect signature`

Any other stderr line, including an extra unknown line after a known diagnostic,
is non-definitive. Infrastructure diagnostics always take precedence, including
`memory allocation failed`, `libcrypto error: x`, `error in libcrypto`,
`Unable to open allowed keys file F: Too many open files`,
`Unable to open allowed keys file F: Input/output error`, and
`Couldn't read signature file: Input/output error`; arbitrary strerror text is
never allowlisted. Unsafe prechecks, timeouts, signals, other exit codes,
missing/extra stdout and all other ambiguous results remain non-definitive.
Their rows stay unchecked, capturable and retryable without restart. Existing
infrastructure-precedence assertions remain in place.

Real ssh-keygen regressions must prove wrong-key, truncated and wrong-namespace
signatures are cached invalid without verification on the next maintenance
pass. With this OpenSSH version an empty allowed-signers file or a directory
used as allowed-signers can produce exit 255, exact rejection stdout and empty
stderr, just like a wrong key: those outputs are deliberately definitive invalid
under this contract. A reported allowed-signers or signature-file I/O diagnostic
is non-definitive regardless of its strerror. Existing constructor checks still
reject a directory already present at authority creation; the output contract
also covers a path changed after that check. No new signer-path precheck is
part of this fix.

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
