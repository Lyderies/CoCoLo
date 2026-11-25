# Assertion changes

No pre-existing assertion is changed or removed.

New scenario assertion correction during development:
`record == repository_record` -> equality of every non-underscore field on both
records. `ProjectRepository.records()` adds `_source_sequence` and `_late` read
annotations; these are not signed record fields. This preserves full signed
content equality while allowing documented repository annotations. Retention,
record-ID, signature-row and reference-count checks remain separate.

The new public-claim scenario is factored into two named checks: the synchronous
public reference service (real SSH verification, with the limit-two capture at
its preparation boundary), and the original deferred service with real sandboxed
helpers. All original scenario assertions are retained in the shared helper.
The synchronous check also explicitly captures and compares admission inputs.
The deferred check remains required for P9-1; sandbox failure is not reported as
successful deferred execution.

New scenario oracle correction: checking the last persisted `admission_decision`
for the latest attestation digest -> observing the real `evaluate_launch` call
and requiring a successful public claim with usable Codex credits. Claims only
write an admission decision on denial; the previous assertion examined the
run's older open-time decision. The replacement checks the claim-time verified
attestation inputs, and usable credits make success depend on the latest tied
attestation's `codex_credits_spending_disabled=True`; the older, foreign and
invalid-newer attestations lack that waiver.

New mutation fixture correction: UPDATE of append-only owner history -> append a
new owner-generation row, require overflow until maintenance refreshes it, then
require the same restart/rebuild checks. The append-only enforcement is kept.

The new owner-generation fixture waits for two bounded passes (finish the current
cursor, then refresh) instead of assuming one sweep resets a cursor already at
the end of its page. Capture remains refused while that refresh is pending.

The new scenario now runs `CollectorServer._sweep_loop()` with a deterministic
clock/wait in place of twelve direct `_sweep_cycle()` calls. All removal and
retention assertions remain; an added elapsed-clock assertion proves the
collector schedules twelve full maintenance intervals. Unrelated health
reporting is substituted, and no socket listener is required for this proof.
