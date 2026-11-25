# Assertion changes

R1 changes are listed below. Earlier round corrections are retained for audit.

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

R1 MED2 replacement: the transient-failure test's verifier `return_value=False`
and empty capture -> `TimeoutExpired` and the original row remains charged.
Its recovered-verifier retry assertions are retained. A definitive invalid
verdict is now cached for exact inputs/generation as explicitly required by
the R1 scope; separate new assertions require 100 invalid calls outside both
lock and transaction, and no more calls on subsequent passes.

New scale-fixture correction: three owner_attestations values -> four, including
attested_at_ms=0, matching the existing schema. No acceptance assertion changed.

New scale-fixture setup: 102 maintenance calls of batch 100 -> one setup call
of batch 10001. This avoids repeatedly exercising the separate known due-query
scan defect while preparing the capture comparison; LOW1 has its own bounded
batch-10 cursor test. Both scale measurements warm each statement once before
counting VM steps, excluding one-time prepare/reprepare work equally. All
selection, retained-count and 100-step-difference assertions are unchanged.

New scale test strengthened: two traced signature SELECTs plus one rowid-only
admission SELECT -> four traced production SELECTs (signature headers/body and
admission headers/body), plus matching selected record IDs at both public
capture boundaries. The query-count assertion changes 2 -> 4 to enforce this.
The retained-history and VM-difference assertions stay unchanged.

R1 MED1 budget-fixture replacements (pre-existing tests):
- test_unrelated_history_scan_exhausts_work_before_body_copy_then_recovers:
  5,000 unrelated event rows with default row limit -> 201 unchecked attestation
  candidates with row limit 201. The work-limit reason, no body-copy call,
  recovered exact rows, no writes and restored busy_timeout assertions remain.
- test_capture_deadline_refuses_during_scan_and_restores_the_owner: same fixture
  replacement; deadline refusal, no body copy, observed progress callbacks,
  cleared scope, usable connection and exact recovery assertions remain.
Reason: P9-4 now requires indexed capture, so unrelated event rows are no
longer a contractual source of scan work. Unchecked candidates remain charged,
and still exercise both budget mechanisms before copying. No assertion is
removed or relaxed. The first test's historical name is retained.

MED2 timeout injection strengthened: mock the real verifier's subprocess.run
with TimeoutExpired, rather than mocking verify_owner_record to raise. The
row-stays-charged and successful later-retry assertions are unchanged. This
exposed the lower-level False conversion; R1 adds an opt-in propagation mode
used only by the sweep. Ordinary verifier callers keep their existing behavior.
Additional assertions cover generation/type changes during verification, direct
signature deletion, and eventual deletion beyond a cited cursor prefix.
