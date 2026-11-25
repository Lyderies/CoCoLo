# RS-F S3: resumable hold adoption

Basis: migration analysis section 4/S3; run-supervisor design section 9;
owner rule 129.3; accepted S2 at `d33859cc` on 0.8.60.

**RSF-S3-1 — Durable bounded progress.** Adopt historical account slots,
bindings and probes, and project leases, reservations, contexts, custody and
orchestrations by class, seeking a durable source cursor. Each adoption batch visits at
most 64 source rows. Its writes and cursor commit together. Resume skips
committed batches and retries an uncommitted batch. A migration whose entire
adoption fits in 64 source rows may retain one atomic transaction. Freeze
classification time across resumptions. Historical records remain upgradeable.
The first transaction's legacy backfills for account ledgers below 17 and the
final transaction's account release-due sweep and full account/project validators
are **not batch-bounded**. These transactions are atomic and idempotent on resume;
the 64-row bound applies to adoption, not to the entire upgrade.

**RSF-S3-2 — Fence every batch.** Retain S2's lifetime writer lock and claim
the runtime_takeover epoch before adoption. Check ownership and epoch at each
batch transaction and commit. A dead owner releases its lock; a successor
claims a fresh epoch. Revocation must roll back the pending batch and cursor.
Preserve the authorization and verified backup gates on the original upgrade.
Exercise a real successor process after SIGKILL, assert its epoch increments on
resume, and prove a retained stale owner cannot commit adoption rows and cursor.

**RSF-S3-3 — Validate before exposure.** Pending migration metadata prevents
ordinary startup from serving a partial register. Only an explicitly authorized
owner may resume it. Before the first durable batch, install the existing
target version to exclude old executables. This version is an exclusion fence,
not publication: remove the pending marker only with successful register,
custody and schema validation. Keep the original project pre-upgrade copy
through an interrupted migration. A failure before the first checkpoint keeps
the existing rollback behavior; after it, retain progress for retry.
Pending refusals give the plain next step and `Run: ./setup.sh --update`.
Reset the connection's temporary migration context even if adoption or validation
raises. Before resuming a project, require the kept before-copy named by the
original attempt marker; refuse plainly if that recovery copy is missing.

**RSF-S3-4 — Process-death equivalence.** Use real subprocesses and SIGKILL
immediately before and after a batch commit, including a second interruption
after resume, the last short batch / `done=1` commit for every class, and the
publication commit containing progress-table removal after full validation.
Reopen twice and compare all historical identities, row counts,
signed/sealed bytes and adopted rows against an uninterrupted upgrade from
the same source. Verify durable cursors at the kill boundary, class coverage,
bounded batches, failed validation and revoked ownership.

**RSF-S3-5 — Stable final formats.** Reuse ledger 17 and project 22. Temporary
progress metadata exists only during migration and disappears atomically on
publication; no permanent schema step, VERSION or runtime catalogue bump is
needed. Keep progress out of backups and source images by withholding the
owner until validation. Exercise the source-image reader and named copy,
backup, restore, move and record-enumeration modules. Register scenario tests
and prove `metadata_errors: []`. Record evidence and limitations in
[STATUS.md](rsf-s3/STATUS.md); pair any existing assertion edits in
[assertions.md](rsf-s3/assertions.md).

## Recovery after a checkpoint

If final validation fails after the first checkpoint, the store stays pending
and ordinary startup refuses it. Keep services drained and preserve the pending
database and recovery material. Fix the cause and run `./setup.sh --update` to
retry the atomic final transaction against the committed adoption checkpoints.
Do not delete progress metadata or lower the schema version to force startup.

If validation cannot succeed, recover the project from its kept
`history.sqlite3.before-22.*` copy (the original attempt marker identifies it),
or recover the account using its verified pre-upgrade account backups through
the supported recovery flow. Restore under the drained owner procedure, verify
the recovered store, and rerun the authorized update. A missing project copy
must be recovered from safe storage at its original name before resume; an
unrelated before-copy does not replace it. Keep the failed database for diagnosis.
