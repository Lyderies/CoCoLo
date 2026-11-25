# RS-F S6 assertion audit

No existing assertion has been changed or removed. New tests exercise the owner
policy in decisions 129.2/129.3; fresh-store other-runtime refusals remain required.

New-test correction, before final green: the first pre-commit SIGKILL originally
asserted a pending table with a null lease cursor. Replacement: assert schema 18
and absence of the pending table. S3 explicitly rolls back the entire upgrade
before its first checkpoint. Later pre-commit cursors and every post-commit
cursor retain their original assertions; two-reopen full-store equality remains.
No pre-existing test assertion changed. The pending-open fixture now deliberately
omits stored inputs through the old reserve API, matching its named missing-input
case; no database row or test assertion was altered for that fixture correction.
