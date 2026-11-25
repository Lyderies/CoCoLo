# Existing assertion audit

No existing assertion changed or removed.

During S3's new test development, the historical account record comparison
changed from whole pre-16 rows equal whole current rows to equality of every
original column plus an explicit NULL assertion on the added `probe_id` column.
Reason: the existing 14→17 migration adds that nullable column; historical signed
bytes must remain equal, while the row width must reflect the accepted schema.
The account fixture also supplies the observation defaults already present in
genuine schema-14 bindings, so the ordinary current-schema reopen's existing
default repair does not turn an artificial fixture into a different source.
No pre-existing test or assertion was edited.

The new progress-row tuple assertions gained `clock_reference_ns` (10 for the
account fixture, NULL for project). They still assert the same store, class,
cursor 64, incomplete flag and original wall time, and now also assert the
monotonic reading whose omission the clock-change regression caught red.

The new account fixture now includes a real detached SSH owner signature and
asserts that the nonempty `owner_attestations` rows survive byte for byte.
This strengthens the preservation test without replacing any assertion.

## Review correction round

No assertion from the four original S3 acceptance methods was changed or removed.
The subprocess harness's readiness, boundary and exact SIGKILL assertions moved
unchanged into shared helpers so the same real-process checks cover the added
short/done/publication and successor cases. The harness additionally reports the
actual epoch, process identity and successful validators at publication.

All six added test methods add assertions. They cover the last two-row batch's
cursor and done=1 on both sides of COMMIT for every class, publication before/after
DROP with successful full validators, full-store equality after two reopens,
real successor epochs and a rolled-back stale row/cursor commit, four exception
cleanup paths, actionable refusal messages, and exact-marker recovery-copy checks.
Missing copy, missing marker and symlink cases must refuse without changing rows
or epoch, even with an unrelated copy present; restoring the original material
allows resume. Failed project validation must retain its original copy byte for
byte through retry. No old → new assertion replacement is needed.
